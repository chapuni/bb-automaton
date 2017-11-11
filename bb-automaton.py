#!/usr/bin/python

import json
import os
import re
import subprocess
import sys
import time

from urllib import *

from branchmanager import BranchManager
from cmds import *
from gitutil import *
from revertmanager import RevertManager

bb_url = 'http://localhost:8010/'
if len(sys.argv) >= 2:
    bb_url = sys.argv[1]

git_dir = '/home/chapuni/bb-automaton/llvm-project'

api_url = bb_url+'api/v2/'
change_url = bb_url+"change_hook/base"
upstream_commit = "origin/master" # May be overridden by test/master

# Post the commit
def post_commit(commit):
    if False:
        print("Dry run -- r%d" % svnrev)
        return False

    resp=urlopen(change_url, urlencode(commit))
    for line in resp:
        print(line.rstrip())
    st=resp.getcode()
    assert st == 200, "status=%d" % st
    resp.close()
    return True

def get_recentbuilds(builderid=None, limit=24):
    q = {
        "order": "-buildid",
        "property": "*",
        "limit": limit,
        }

    if builderid is not None:
        q["builderid"] = builderid

    resp = urlopen(api_url+'builds?'+urlencode(q))

    recentbuilds = {}
    for a in json.load(resp)["builds"]:
        builderid = a["builderid"]
        if builderid not in recentbuilds:
            recentbuilds[builderid] = []
        recentbuilds[builderid].append(a)

    resp.close()
    return recentbuilds

def get_culprit_ss(builder):
    recentbuilds[builderid] = get_recentbuilds(builderid, limit=256)[builderid]
    blamed = False
    found_ss = None
    max_rev = None
    max_ss = None
    for i,brd in enumerate(recentbuilds[builderid]):
        if brd["results"] > 2:
            continue

        if "blamed" in brd["properties"]:
            prop_blamed = json.loads(brd["properties"]["blamed"][0])
            if prop_blamed.get("event") == "BLAME":
                blamed = prop_blamed["revision"]

        if brd["results"] in (0, 1):
            if "bisect" in brd["properties"]:
                continue
            else:
                break

        if "result_edge" not in brd["properties"]:
            continue

        print("brd=%d %s" % (brd["buildid"], brd["properties"]["result_edge"]))
        resp = urlopen(api_url+'buildrequests?'+urlencode({
                    "buildrequestid": brd["buildrequestid"],
                    }))
        breqs = json.load(resp)["buildrequests"]
        resp.close()
        for breq in breqs:
            resp = urlopen(api_url+'buildsets?'+urlencode({
                        "bsid": breq["buildsetid"],
                        }))
            bsets = json.load(resp)["buildsets"]
            resp.close()
            for bset in bsets:
                for ss in bset["sourcestamps"]:
                    if blamed == ss["revision"] and found_ss is None:
                        found_ss = ss
                    m = re.match('r(\d+)', ss["revision"])
                    if m and (max_rev is None or max_rev < int(m.group(1))):
                        max_ss = ss
                        max_rev = int(m.group(1))
                        print("rev=%d" % max_rev)

    if found_ss:
        print("Culprit is %s (ssid=%d)" % (found_ss["revision"], found_ss["ssid"]))
        return (found_ss, max_ss, max_rev)

    return (None,None,None)

class TopicsManager:
    def __init__(self):
        self._changes = {}

    def changes(self, staged_ref):
        if staged_ref in self._changes:
            return self._changes[staged_ref]

        base = git_merge_base(upstream_commit, staged_ref)
        self._changes[staged_ref] = changes = git_diff_files(base, staged_ref)
        return changes

# Check failures

resp = urlopen(api_url+'builders')
builders = json.load(resp)
resp.close()

recentbuilds = get_recentbuilds(limit=64)

culprit_svnrev = None
culprit_svnrevs = {}
ss_info = {}
first_ss = None
min_green_rev = sys.maxint

for builder in builders["builders"]:
    builderid = builder["builderid"]
    if builderid not in recentbuilds:
        tmpbuilds = get_recentbuilds(builderid)
        if builderid not in tmpbuilds:
            # There's no build.
            print(" (%s)" % builder["name"])
            continue
        recentbuilds[builderid] = tmpbuilds[builderid]

    # Prune in-progress build
    while recentbuilds[builderid]:
        result = recentbuilds[builderid][0].get("results", -1)
        if result < 0:
            recentbuilds[builderid].pop(0)
            continue
        break
    if not recentbuilds[builderid]:
        print(" (%s) in-progress" % builder["name"])
        continue

    # Get last result
    build = recentbuilds[builderid][0]
    result = build.get("results", -1)
    if result is None:
        result = -1

    if result in (0, 1) and "blamed" in build["properties"]:
        # Confirm if it is "the last bis good"
        if json.loads(build["properties"]["blamed"][0]).get("event") == "BLAME":
            result = 2

    if result not in (0,1):
        print("%d : %s" % (result, builder["name"]))

    if result == 2:
        min_green_rev = 0
        ss,max_ss,max_rev = get_culprit_ss(builder)
        if ss is None:
            continue

        ss_info[ss["ssid"]] = dict(
            builderid=builderid,
            buildid=build["buildid"],
            buildNumber=build["number"],
            builderName=builder["name"],
            revision=ss["revision"],
            max_rev=max_rev,
            )

        m_svnrev = re.match(r'^r(\d+)$', ss["revision"])
        if m_svnrev:
            svnrev = int(m_svnrev.group(1))
            ss_info[ss["ssid"]]["svnrev"] = svnrev
            if svnrev in culprit_svnrevs:
                continue
            culprit_svnrevs[svnrev] = ss

        if first_ss is None or first_ss["ssid"] > ss["ssid"]:
            first_ss = ss
    elif result in (0,1):
        if "revision" not in build["properties"]:
            continue
        # Ignore rXXX+rXXX
        m = re.match(r'^r(\d+)$', build["properties"]["revision"][0])
        if not m:
            continue
        rev = int(m.group(1))
        if min_green_rev > rev:
            min_green_rev = rev
    else:
        min_green_rev = 0

if first_ss:
    print("========Culprit is %s to r%d\n(%s)" % (first_ss["revision"], ss_info[first_ss["ssid"]]["max_rev"], first_ss["project"]))
    m_svnrev = re.match(r'^r(\d+)$', first_ss["revision"])
    if m_svnrev:
        culprit_svnrev = int(m_svnrev.group(1))

branches = BranchManager(
    generated = [
        "graduates/",
        "recommits/",
        "rejected/",
        "reverts/",
        "staged/",
        ],
    ignored = [
        "HEAD",
        "master",
        "test/master",
        ],
    )

# Graduates
if not first_ss and 0 < min_green_rev and min_green_rev < sys.maxint:
    revs = []
    for ref0,grads in branches["graduates"].items():
        m = re.match(r'^r(\d+)', ref0)
        svnrev = int(m.group(1))
        for ref1 in grads.keys():
            m = re.match(r'^r(\d+)', ref1)
            grad_rev = int(m.group(1))
            if grad_rev >= min_green_rev:
                continue
            revs.append("graduates/%s/%s" % (ref0, ref1))
            if ref0 in branches["reverts"]:
                revs.append("reverts/%s" % ref0)
            if ref0 in branches["recommits"]:
                revs.append("recommits/%s" % ref0)
            print("\t%s is graduated to the heaven." % ref0)

    if revs:
        run_cmd(["git", "branch", "-D"] + revs, stdout=True)

reverts = RevertManager(branches)
topics_man = TopicsManager()

for ref in branches["reverts"].keys():
    m = re.match("^r(\d+)", ref)
    if not m:
        continue
    svnrev = int(m.group(1))
    br = branches["reverts"][ref]
    if ref in branches["recommits"]:
        br = branches["recommits"][ref]
    reverts.register(svnrev, br["name"], br["email"])

unstaged_topics = []
staged_topics = {}

if "test" in branches and "master" in branches["test"]:
    upstream_commit = branches["test"]["master"]["h"]

master = branches["master"]["h"]

# Make sure we are alywas on detached head.
run_cmd(["git", "checkout", "-qf", master])

# Seek culprit rev, rewind and revert
invalidated_changes = None
if first_ss is not None:
    do_rewind = False

    for svnrev in sorted(culprit_svnrevs.keys()):
        ss = culprit_svnrevs[svnrev]
        svn_commit = git_merge_base(ss["project"], upstream_commit)
        orig_commit = collect_single_commit(svn_commit)
        author = orig_commit["author"]
        m = re.match(r'^(.+)\s<([^>]*)>$', author)
        name = m.group(1)
        email = m.group(2)
        ss_info[ss["ssid"]].update(dict(
                author=author,
                name=name,
                email=email,
                ))

    svn_commit = git_merge_base(first_ss["project"], upstream_commit)

    if culprit_svnrev is not None:
        revert_ref = reverts.refspec(culprit_svnrev)
        # Confirm if the revert exists.
        if (eval_cmd(["git", "rev-parse", "--verify", "-q", revert_ref])
            or eval_cmd(["git", "rev-parse", "--verify", "-q", "graduates/r%d" % culprit_svnrev])):
            print("%s exists. Do nothing." % revert_ref)
        else:
            do_rewind = True

            # Rewind master to one commit before the revertion.
            master = "%s^" % first_ss["project"]
            run_cmd(["git", "branch", "-f", "master", master])

            # Make "reverts" commits.
            for svnrev in reversed(sorted(culprit_svnrevs.keys())):
                ss = culprit_svnrevs[svnrev]
                head = ss["project"]
                svn_commit = git_merge_base(head, upstream_commit)
                print("head=%s master=%s svn=%s" % (head, master, svn_commit))
                si = ss_info[ss["ssid"]]
                revert_h = reverts.revert(svn_commit, svnrev, head, bba=ss_info[ss["ssid"]])
                reverts.make_recommit(svn_commit, svnrev, revert_h, name=si["name"], email=si["email"])
    else:
        # Doesn't revert. Just skip.

        # Confirm if the revert exists.
        if False:
            print("%s exists. Do nothing." % revert_ref)
        else:
            do_rewind = True
            # Rewind master to one commit before the revertion.
            master = "%s^" % first_ss["project"]
            print("master=%s svn=%s" % (master, svn_commit))

            run_cmd(["git", "branch", "-f", "master", master])

    if do_rewind:
        # Remain incoming "graduated" commits.
        m = re.match(r'^r(\d+)', first_ss["revision"])
        assert m
        svnrev = int(m.group(1))
        remains = []
        for grad_ref,grads in branches["graduates"].items():
            for grad_at in grads.keys():
                m = re.match(r'r(\d+)', grad_at)
                assert m
                if int(m.group(1)) < svnrev:
                    continue
                ref = "graduates/%s/%s" % (grad_ref, grad_at)
                print("\t%s remains." % ref)
                remains.append(ref)

        if remains:
            run_cmd(["git", "branch", "-D"] + remains, stdout=True)

        # Calculate range(ssid) to invalidate previous builds
        resp = urlopen(api_url+'changes?project=%s' % first_ss["project"])
        changes = json.load(resp)
        resp.close()
        ch_a = changes["changes"][0]["changeid"]
        # Get the latest change.
        resp = urlopen(api_url+'changes?limit=1&order=-changeid')
        changes = json.load(resp)
        resp.close()
        ch_b = changes["changes"][0]["changeid"]
        invalidated_changes = "%d..%d" % (ch_a, ch_b)
        print("========Rewind to r%d (Invalidate %s)" % (svnrev, invalidated_changes))
else:
    # FIXME: Seek diversion of upstream
    pass

suppressed_recommits = {}

for si in ss_info.values():
    if "name" in si and "email" in si:
        suppressed_recommits[si["name"]] = suppressed_recommits[si["email"]] = si["max_rev"]

# Collect commits from git-svn
p = subprocess.Popen(
    [
        "git", "log",
        "--reverse",
        "--format=raw", "--show-notes",
        "--stat=1024,1000",

        "master..%s" % upstream_commit,
        ],
    stdout=subprocess.PIPE,
    )

last_svnrev = None

for commit in collect_commits(p.stdout):
    svn_commit = commit["commit"]
    m = re.match('^r(\d+)', commit["revision"]) # rNNNNNN
    assert m
    last_svnrev = svnrev = int(m.group(1))
    props={
        "commit": svn_commit,
        }
    del commit["commit"]

    bba = {}

    m = re.match(r'^(.+)\s<([^>]*)>$', commit["author"])
    author_name = m.group(1)
    author_email = m.group(2)

    # FIXME: Invalidate ssid with api.
    if invalidated_changes is not None:
        props["invalidated_changes"] = invalidated_changes

    print("========Processing r%d" % svnrev)

    chain_recommit = None

    # Check graduation
    # FIXME: Skip if change is nothing to do.
    graduated = []
    for revert_svnrev in list(reverts):
        if revert_svnrev > svnrev:
            continue
        revert_ref = reverts.refspec(revert_svnrev)

        # Don't check if each revert doesn't touch the commit.
        if not commit["files"].intersection(reverts.changes(revert_svnrev)):
            print("\tgrad: Skipping %s" % revert_ref)
            continue

        print("\tgrad: Checking %s" % revert_ref)
        git_reset(svn_commit)
        if not eval_cmd(["git", "merge", "--squash", revert_ref]):
            continue
        if not eval_cmd("git diff --quiet --cached"):
            continue
        # Merge isn't affected. Assume graduated.
        print("\tgrad: %s is graduated." % revert_ref)

        # Make grad commit.
        r = do_merge([revert_ref], name=author_name, email=author_email)
        assert r
        graduated.append(git_head())

        reverts.may_graduate(svnrev, revert_svnrev)

    # Check graduation for staged topics
    for topic_svnrev,topics in staged_topics.items():
        if topic_svnrev > svnrev:
            continue

        for topic in list(topics):
            staged_ref = "staged/%s.r%d" % (topic, topic_svnrev)

            # Don't check if each revert doesn't touch the commit.
            if not commit["files"].intersection(topics_man.changes(staged_ref)):
                print("\tgrad: Skipping %s" % staged_ref)
                continue

            print("\tgrad: Checking %s" % staged_ref)
            git_reset(svn_commit)
            if not eval_cmd(["git", "merge", "--squash", staged_ref]):
                continue
            if not eval_cmd("git diff --quiet --cached"):
                continue
            # Merge isn't affected. Assume graduated.
            print("\tgrad: %s is graduated." % staged_ref)
            topics.remove(topic)
            run_cmd(["git", "branch", "-D", staged_ref], stdout=True)
            run_cmd(["git", "push", "dev", ":%s" % staged_ref])

    git_reset(master)

    # Apply reverts
    local_reverts = []
    if svnrev in reverts:
        print("\trevert: Checking r%d" % svnrev)
        local_reverts.append(reverts.refspec(svnrev))
        assert local_reverts
        if do_merge(local_reverts, ff=False, name=author_name, email=author_email):
            print("\trevert: Applied %s" % str(local_reverts))
            commit["files"]=set()
        else:
            print("\trevert: Local reverts failed. %s" % str(local_reverts))
            git_reset(master)
            reverts.remove(svnrev)

    # Apply svn HEAD
    if graduated:
        print("\tgrad: Applying graduated commit: %s" % graduated)
        assert do_merge(graduated, ff=False)
    elif not local_reverts:
        # Check suppression of recommit
        k = None
        suppress_recommit = False
        if author_name in suppressed_recommits:
            k = author_name
        if k is None and author_email in suppressed_recommits:
            k = author_email
        if k is not None and svnrev <= suppressed_recommits[k]:
            #print("\tRecommit <%s> is suppressed until r%d." % (author_name, suppressed_recommits[k]))
            suppress_recommit = True

        print("\tApplying r%d..." % svnrev)

        if do_merge([svn_commit], ff=True, msg="Merged r%d" % svnrev, stdout=True, name=author_name, email=author_email):
            # if files are present but commit is empty, check graduation.
            head = git_head()
            if commit["files"]:
                reverts.check_graduated(svnrev, svn_commit)
                git_reset(head)

            if not suppress_recommit:
                interests = []
                for ref,names,emails in reverts.gen_recommits(svnrev, want_tuple=True):
                    if author_name in names or author_email in emails:
                        interests.append(ref)

                if interests:
                    chain_recommit = reverts.gen_recommits_cands(svn_commit, svnrev, author_name, author_email)
                    git_reset(head)
                    bba["soft"] = True

        else:
            cands = []
            cand_revs = []
            msg = commit["comments"]
            if not suppress_recommit:
                cands = reverts.gen_recommits_cands(svn_commit, svnrev, author_name, author_email)
                git_reset(master)

                for cand in cands:
                    m = re.match(r'^recommits/r(\d+)', cand) # FIXME: Confirm it works.
                    cand_revs.append(int(m.group(1)))

                msg = "[Recommit %s] %s" % (','.join(map(lambda rev: "r%d" % rev, cand_revs)), msg)

            if not suppress_recommit and do_merge(cands + [svn_commit], msg="r%d: %s" % (svnrev, msg), stdout=True):
                commit["comments"] = msg
                print("\tApplied r%d with %s" % (svnrev, str(cands)))
                for rev in cand_revs:
                    reverts.may_graduate(svnrev, rev)
                # FIXME: Mark proerty as it is synthesized
            else:
                # This doesn't affect to build.
                revert_bba = dict(
                    author=commit["author"],
                    name=author_name,
                    email=author_email,
                    revision=commit["revision"],
                    svnrev=svnrev,
                    )
                # Chain revert
                revert_h = reverts.revert(svn_commit, svnrev, master, bba=revert_bba)
                commit["files"]=set()
                revert_ref = reverts.refspec(svnrev)
                git_reset(master)
                # FIXME: Add message
                assert do_merge([revert_ref], name=author_name, email=author_email)
                print("\tApplied new %s" % revert_ref)

                # Make recommits
                head = git_head()
                reverts.make_recommit(svn_commit, svnrev, head, name=author_name, email=author_email)
                git_reset(head)

    bba.update(reverts.build_status(svnrev))
    if bba:
        print(json.dumps(bba, indent=2))
        props["bba"] = bba

    # Make actual changes
    commit["files"] = git_diff_files(master)
    master = git_head()

    props["commit"] = master
    commit["properties"]=json.dumps(props)
    commit["files"]=json.dumps(sorted(commit["files"]))

    # XXX Hack
    commit["project"] = master

    # Post the commit
    if post_commit(commit):
        run_cmd(["git", "branch", "-f", "master", master])

    # Recommit chained by author
    if chain_recommit:
        msg = "Recommit: %s" % str(chain_recommit)

        git_reset(master)
        print("\tRecommit for %s: %s" % (author_name, str(chain_recommit)))
        assert do_merge(chain_recommit, stdout=True, name=author_name, email=author_email, msg=msg)

        head = git_head()
        # FIXME: Mark it synthesized.
        m = re.match(r'recommits/(.+)', chain_recommit[-1])
        commit = {
            "comments": msg,
            "revision": "r%d+%s" % (svnrev, m.group(1)),
            "revlink": "",
            "when": int(time.time()),
            "author": "%s <%s>" % (author_name, author_email),
            "files": set(),
            "project": head,
            "branch": "master",
            "repository": "",
            "category": "",
            "codebase": "",
            "properties": {"commit": head},
            }

        # FIXME: Invalidate ssid with api.
        if invalidated_changes is not None:
            commit["properties"]["invalidated_changes"] = invalidated_changes

        commit["files"]=git_diff_files("master")
        if commit["files"]:
            commit["properties"]=json.dumps(commit["properties"])
            commit["files"]=json.dumps(sorted(commit["files"]))

            master = git_head()
            if post_commit(commit):
                print("\tRecommit for %s: done." % author_name)
                for recommit in chain_recommit:
                    m = re.match(r'^recommits/r(\d+)', recommit)
                    if m:
                        reverts.may_graduate(svnrev, int(m.group(1)))
                run_cmd(["git", "branch", "-f", "master", master])
        else:
            print("\tRecommit for %s: (skipped due to empty commit)" % author_name)

    # Push past-staged topics
    if svnrev in staged_topics:
        topics_svnrev = svnrev

        for topic in list(staged_topics[topics_svnrev]):
            staged_ref = "staged/%s.r%d" % (topic, topics_svnrev)
            print("\t%s: Merging..." % staged_ref)

            cands = attempt_merge(staged_ref, list(reverts.gen_recommits(topics_svnrev)))

            git_reset(master)
            if not do_merge([staged_ref] + cands):
                # Reject
                rejected_ref = "rejects/topic"
                run_cmd(["git", "branch", "-M", staged_ref, rejected_ref])
                run_cmd(["git", "push", "dev",
                         # Remove staged
                         ":%s" % staged_ref,
                         # Push rejected
                         "+%s:%s" % (rejected_ref, rejected_ref),
                         ], stdout=True)
                # Unregister
                staged_topics[topics_svnrev].remove(topic)
                print("\t%s: => %s" % (staged_ref, rejected_ref))
                continue

            # Retrieve original message
            commit = collect_single_commit(staged_ref)

            # FIXME: Invalidate ssid with api.
            if invalidated_changes is not None:
                commit["properties"]["invalidated_changes"] = invalidated_changes

            commit["revision"] = "dev/%s" % topic
            commit["revlink"] = "https://github.com/llvm-project/llvm-project-dev/commits/%s" % staged_ref

            # Get diff
            commit["files"] = topics_man.changes(staged_ref)

            master = git_head()
            commit["properties"]["commit"] = git_head()
            commit["project"] = master

            commit["properties"]=json.dumps(commit["properties"])
            commit["files"]=json.dumps(sorted(commit["files"]))

            if post_commit(commit):
                run_cmd(["git", "branch", "-f", "master", master])
                print("\t%s: Successfully merged." % staged_ref)

p.wait()

# Get the latest svnrev
# (Note, collect_commits may return [])
if last_svnrev is None:
    # Retrieve origin/master
    commit = collect_single_commit(upstream_commit)
    m = re.match(r'r(\d+)', commit["revision"])
    assert m, commit
    last_svnrev = int(m.group(1))

# Pick up topics
#   remotes/dev/topic
#   rejects/topic
#   staged/topic.rXXXXXX
for topic in unstaged_topics:
    print("\tTopic: %s" % topic)
    topic_ref = "remotes/dev/%s" % topic
    rejected_ref = "rejects/%s" % topic
    staged_ref = "staged/%s.r%d" % (topic, last_svnrev)

    # confirm if it applies to master with recommits
    git_reset("master")
    cands = attempt_merge(topic_ref, list(reverts.gen_recommits()))

    git_reset("master")
    if not do_merge(cands + [topic_ref]):
        # If it isn't mergeable, move it to "rejects/"
        print("\tTopic: Reject %s" % topic)
        run_cmd(["git", "branch", "-f", rejected_ref])
        run_cmd(["git", "push", "dev",
                 ":%s" % topic,
                 "+refs/remotes/dev/%s:refs/heads/%s" % rejected_ref])
        continue

    # Retrieve original message
    commit = collect_single_commit(topic_ref)

    # FIXME: Invalidate ssid with api.
    if invalidated_changes is not None:
        commit["properties"]["invalidated_changes"] = invalidated_changes

    commit["revision"] = "dev/%s" % topic
    commit["revlink"] = "https://github.com/llvm-project/llvm-project-dev/commits/%s" % staged_ref
    commit["properties"]["commit"] = git_head()

    # Get diff
    commit["files"] = git_diff_files("master")

    commit["properties"]=json.dumps(commit["properties"])
    commit["files"]=json.dumps(sorted(commit["files"]))

    master = git_head()
    commit["project"] = master

    if post_commit(commit):
        run_cmd(["git", "branch", "-f", "master", master])

        run_cmd(["git", "branch", "-f", staged_ref, topic_ref])
        run_cmd(["git", "push", "dev",
                 # Remove original
                 ":%s" % topic,
                 # Push staged_ref
                 "+%s:%s" % (staged_ref, staged_ref),
                 ], stdout=True)

#EOF
