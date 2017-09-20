#!/usr/bin/python

import json
import os
import re
import subprocess
import sys
import time

from urllib import *

bb_url = 'http://localhost:8010/'
if len(sys.argv) >= 2:
    bb_url = sys.argv[1]

git_dir = '/home/chapuni/bb-automaton/llvm-project'

api_url = bb_url+'api/v2/'
change_url = bb_url+"change_hook/base"
upstream_commit = "origin/master" # May be overridden by test/master

def re_match(expr, line, r):
    m = re.match(expr, line)
    r["m"] = m
    return m

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
    for i,brd in enumerate(recentbuilds[builderid]):
        if brd["results"] in (0, 1):
            return None
        if brd["results"] != 2:
            continue

        if brd["properties"]["result_edge"][0] == "succ2fail(1)":
            resp = urlopen(api_url+'buildrequests?'+urlencode({
                        "buildrequestid": brd["buildrequestid"],
                        }))
            breqs = json.load(resp)["buildrequests"]
            resp.close()
            revs=[]
            first_ss = None
            for breq in breqs:
                resp = urlopen(api_url+'buildsets?'+urlencode({
                            "bsid": breq["buildsetid"],
                            }))
                bsets = json.load(resp)["buildsets"]
                resp.close()
                for bset in bsets:
                    #if i > 0 and bset["reason"] != "bisect":
                    #    continue
                    print("len(ss)=%d reason=<%s>" % (len(bset["sourcestamps"]), bset["reason"]))
                    for ss in bset["sourcestamps"]:
                        if ss["revision"] not in revs:
                            first_ss = ss
                            revs.append(ss["revision"])
            if len(revs) == 0:
                continue
            assert first_ss is not None
            print("Culprit is %s (ssid=%d)" % (first_ss["revision"], first_ss["ssid"]))
            return first_ss

    return None

# Oneliner expects success.
def eval_cmd(args, stdout=False, stderr=False, report=False, name=None, email=None):
    if isinstance(args, str):
        args = args.split()

    env = os.environ
    if name is not None and email is not None:
        env = dict(os.environ, GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email)

    p = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        )
    o = ''.join(p.stdout.readlines())
    e = ''.join(p.stderr.readlines())
    if p.wait() == 0:
        if stdout:
            sys.stdout.write(o)
        if stdout or stderr:
            sys.stderr.write(e)
        return True
    else:
        if report:
            sys.stdout.write(o)
            sys.stderr.write(e)
        return False

# Oneliner expects success.
def run_cmd(args, **kwargs):
    assert eval_cmd(args, report=True, **kwargs)

def git_reset(head="HEAD"):
    run_cmd(["git", "reset", "-q", '--hard', head])

# Oneliner expects success.
def git_head(ref="HEAD"):
    p = subprocess.Popen(
        ["git", "rev-parse", ref],
        stdout=subprocess.PIPE,
        )
    m = re.match(r'^([0-9a-f]{40})', p.stdout.readline())
    assert m
    p.wait()
    return m.group(1)

def git_merge_base(*args):
    p = subprocess.Popen(
        ["git", "merge-base"] + list(args),
        stdout=subprocess.PIPE,
        )
    m = re.match(r'^([0-9a-f]{40})', p.stdout.readline())
    assert m
    p.wait()
    return m.group(1)

# Get file list between commits.
def git_diff_files(commit, commit2="HEAD"):
    p = subprocess.Popen(
        ["git", "diff", "--name-only", commit, commit2],
        stdout=subprocess.PIPE,
        )

    changes = set()
    for line in p.stdout:
        changes.add(line.rstrip())
    assert p.wait() == 0

    return changes

def do_merge(commits, msg=None, ff=False, commit=True, **kwargs):
    cmdline = ["git", "merge"]
    if not commit:
        cmdline.append("--no-commit")
    if not ff:
        cmdline.append("--no-ff")
    if msg:
        cmdline += ["-m", msg]

    return eval_cmd(cmdline + commits, **kwargs)

# This depends on "master"
def attempt_merge(commit, cands):
    i = 0
    while cands and i < len(cands):
        target_rev = cands[i]
        cand = cands[:]
        cand.pop(i)
        git_reset(master)
        r = do_merge(cand + [commit], commit=False)
        if r:
            print("\tRecommit: <%s>: Removing should be safe." % target_rev)
            cands.pop(i)
        else:
            # Failed. Try next.
            print("\tRecommit: <%s>: It was essential. " % target_rev)
            i += 1

    return cands

# Revert controller
class RevertController:
    def __init__(self):
        # Order by reverse revs
        self._svnrevs = []

        # rev:set() changed files
        self._changes = {}

        # Authors by rev
        self._names = {}
        self._emails = {}

    # For iterator
    def __iter__(self):
        return self._svnrevs.__iter__()

    def __next__(self):
        return self._svnrevs.__next__()

    def __getitem__(self, i):
        return self._svnrevs[i]

    def __contains__(self, a):
        return a in self._svnrevs

    @staticmethod
    def refspec(svnrev):
        return "reverts/r%d" % svnrev

    @staticmethod
    def refspec_m(svnrev):
        return "recommits/r%d" % svnrev

    def changes(self, svnrev):
        assert svnrev in self._svnrevs
        if svnrev in self._changes:
            return self._changes[svnrev]

        refspec = self.refspec(svnrev)
        self._changes[svnrev] = changes = git_diff_files(refspec, "%s^" % refspec)
        return changes

    def register(self, svnrev, name, email):
        if svnrev in self._svnrevs:
            self._svnrevs.remove(svnrev)
        self._svnrevs.append(svnrev)

        if svnrev not in self._names:
            self._names[svnrev] = set()
        if name is not None:
            self._names[svnrev].add(name)

        if svnrev not in self._emails:
            self._emails[svnrev] = set()
        if email is not None:
            self._emails[svnrev].add(email)

        # FIXME: Do smarter!
        self._svnrevs.sort(key=lambda x: -x)

    def remove(self, svnrev):
        self._svnrevs.remove(svnrev)
        run_cmd(["git", "branch", "-D", self.refspec(svnrev), self.refspec_m(svnrev)], stdout=True)

    # This moves HEAD
    def revert(self, svn_commit, svnrev, master, msg=None, name=None, email=None):
        git_reset(svn_commit)

        if msg:
            run_cmd(["git", "revert", "--no-commit", svn_commit])
            cmdline = ["git", "commit", "-m", msg]
        else:
            cmdline = ["git", "revert", "--no-edit", svn_commit]

        env = os.environ
        if name is not None and email is not None:
            env = dict(os.environ, GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email)

        p = subprocess.Popen(
            cmdline,
            stdout=subprocess.PIPE,
            )

        line = ''.join(p.stdout.readlines())
        m = re.match(r'\[detached HEAD\s+([0-9a-f]+)\]', line)
        assert m, "git-revert ====\n%s====" % line
        p.wait()

        revert_h = m.group(1)

        self.register(svnrev, name, email)
        assert len(self._svnrevs) == 1 or self._svnrevs[0] > self._svnrevs[1], "<%s>" % str(self._svnrevs)

        revert_ref = self.refspec(svnrev)
        print("\t*** Revert %s" % revert_ref)

        # At last, make reverts branch.
        run_cmd(["git", "branch", "-f", revert_ref, revert_h])

        return revert_h

    def gen_recommits(self, svnrev=None, names=None, emails=None, want_tuple=False):
        for rev in reversed(self._svnrevs):
            if svnrev is not None and rev >= svnrev:
                break

            if not want_tuple:
                yield self.refspec_m(rev)
                continue

            if rev not in self._names:
                self._names[rev] = set()

            if rev not in self._emails:
                self._emails[rev] = set()

            if names is None or (self._names[rev] & names):
                yield (self.refspec_m(rev), self._names[rev], self._emails[rev])
                continue

            if emails is None or (self._emails[rev] & emails):
                yield (self.refspec_m(rev), self._names[rev], self._emails[rev])
                continue

    # Make recommit with HEAD.
    # It requires master is already reverted.
    def make_recommit(self, svn_commit, svnrev, master, name, email):
        # Make recommit on revert.
        # FIXME: Update commit log with svnrev
        git_reset(self.refspec(svnrev))
        run_cmd(["git", "cherry-pick", "--no-commit", svn_commit])
        p = subprocess.Popen(
            ["git", "commit", "-m", "Recommit r%d" % svnrev],
            stdout=subprocess.PIPE,
            env = dict(os.environ, GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email),
            )
        line = ''.join(p.stdout.readlines())
        m = re.match(r'\[detached HEAD\s+([0-9a-f]+)\]', line)
        assert m, "git-recommit ====\n%s====" % line
        p.wait()
        recommit_h = m.group(1)
        recommit_ref = self.refspec_m(svnrev)

        # Make sure if it can be applied to the master
        git_reset(master)
        # FIXME: Try a simple case at first!
        recommit_cand = list(self.gen_recommits(svnrev))
        print("\tRecommit r%d: candidates %s" % (svnrev,str(recommit_cand)))

        recommit_cand = attempt_merge(recommit_h, recommit_cand)

        # Create the actual recommit on the revert.
        git_reset(self.refspec(svnrev))
        msg = "Merge %s" % recommit_ref
        if recommit_cand:
            msg += " with " + ", ".join(recommit_cand)
        recommit_cand.append(recommit_h)
        print("\tRecommit r%d: %s" % (svnrev, msg))
        r = do_merge(recommit_cand, msg=msg, ff=True, name=name, email=email)
        assert r

        self.register(svnrev, name=name, email=email)

        run_cmd(["git", "branch", "-f", recommit_ref, git_head()], stdout=True)

    def check_graduated(self, svn_commit):
        git_reset(svn_commit)
        # Check merge commit (HEAD) is empty
        if not eval_cmd("git diff --quiet HEAD^ HEAD"):
            return

        for svnrev in self._svnrevs:
            revert_ref = self.refspec(svnrev)
            if not do_merge([revert_ref], commit=False, GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email):
                git_reset()
                continue
            if not eval_cmd("git diff --quiet --cached HEAD"):
                continue

            git_reset()
            print("\tr%d has been graduated." % svnrev)
            run_cmd(["git", "branch", "-D", revert_ref, self.refspec_m(svnrev)], stdout=True)

class TopicsManager:
    def __init__(self):
        self._changes = {}

    def changes(self, staged_ref):
        if staged_ref in self._changes:
            return self._changes[staged_ref]

        base = git_merge_base(upstream_commit, staged_ref)
        self._changes[staged_ref] = changes = git_diff_files(base, staged_ref)
        return changes

# git log --format=raw --show-notes
def collect_commits(fh):
    commit = None
    r = {}

    line = fh.readline()
    state = "commit"
    while True:
        if line=="" or line.startswith("commit "):
            if commit is not None:
                yield commit
                commit = None
            if line=="":
                return

        if state=="commit":
            m = re.match(r'^commit\s+([0-9a-f]{40})', line)
            assert m, line
            commit = {
                "commit": m.group(1),
                "comments": "",
                "revision": "",
                "revlink": "",
                "files": set(),
                "project": "",
                "branch": "master",
                "repository": "",
                "category": "",
                "codebase": "",
                "properties": {},
                }
            line = fh.readline()
            state="author"
            continue
        elif state=="author":
            # Discard tree and parent
            if re.match(r'^(tree|parent)\s+', line):
                line = fh.readline()
                continue
            m = re.match(r'^author\s+(.+\s+<[^>]*>)\s+(\d+)', line)
            assert m, line
            commit["author"] = m.group(1)
            commit["when"] = m.group(2)
            state = "committer"
            line = fh.readline()
            continue
        elif state=="committer":
            # Discard committer
            if line.startswith("committer "):
                line = fh.readline()
                continue
            assert line == "\n", "<%s>" % line
            state = "comments"
            line = fh.readline()
            continue
        elif state=="comments":
            while re_match(r'^    (.*)$', line, r):
                commit["comments"] += r["m"].group(1) + "\n"
                line = fh.readline()
                continue
            assert line=="\n", "%d<%s>" % (len(line), line)
            line = fh.readline()
            state="notes"
            continue
        elif state=="notes":
            if not line.startswith("Notes:"):
                state="stat"
                continue
            line = fh.readline()
            if re_match(r'^    git-svn-rev:\s*(\d+)', line, r):
                commit["revision"]="r"+r["m"].group(1)
                commit["revlink"]="https://reviews.llvm.org/rL"+r["m"].group(1)
                line = fh.readline()
            while line.startswith("    "):
                # Discard notes
                line = fh.readline()
                continue
            if line=="":
                continue
            assert line == "\n", "<%s>" % line
            line = fh.readline()
            state="stat"
            continue
        elif state=="stat":
            if re_match("^\s+(\w[^|]+[^ |])\s+\|", line, r):
                while re_match("^\s+(\w[^|]+[^ |])\s+\|", line, r):
                    commit["files"].add(r["m"].group(1))
                    line = fh.readline()
                    continue
                # Discard one line.
                # NN files changed, NN insertions(+), NN deletions(-)
                line = fh.readline()
            if line=="":
                continue
            assert line == "\n", "<%s>" % line
            line = fh.readline()
            state="commit"
            continue
        assert False, "<%s>" % line

# Check failures

resp = urlopen(api_url+'builders')
builders = json.load(resp)
resp.close()

recentbuilds = get_recentbuilds(limit=64)

culprit_svnrev = None
culprit_svnrevs = {}
first_ss = None

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
    result = recentbuilds[builderid][0].get("results", -1)
    if result is None:
        result = -1

    print("%d : %s" % (result, builder["name"]))
    if result == 2:
        ss = get_culprit_ss(builder)
        if ss is None:
            continue

        m_svnrev = re.match(r'^r(\d+)$', ss["revision"])
        if m_svnrev:
            svnrev = int(m_svnrev.group(1))
            if svnrev in culprit_svnrevs:
                continue
            culprit_svnrevs[svnrev] = ss
            # if culprit_svnrev is None or culprit_svnrev > svnrev:
            #     culprit_svnrev = svnrev
            #     first_ss = ss

        if first_ss is None or first_ss["ssid"] > ss["ssid"]:
            first_ss = ss

if first_ss:
    print("========Culprit is %s (%s)" % (first_ss["revision"], first_ss["project"]))
    m_svnrev = re.match(r'^r(\d+)$', first_ss["revision"])
    if m_svnrev:
        culprit_svnrev = int(m_svnrev.group(1))

# Retrieve all branches
p = subprocess.Popen(
    [
        "git", "log",
        "--no-walk",
        "--branches", "--remotes=dev",
        "--format=%aN%H%d%aE",
        ],
    stdout=subprocess.PIPE,
    )

reverts = RevertController()
master = None

unstaged_topics = []
staged_topics = {}
topics_man = TopicsManager()

generated_branches = [
    "HEAD",
    "master",
    "recommits/",
    "rejected/",
    "reverts/",
    "staged/",
    "test/master",
    ]

reverted = {}
recommitted = {}
for line in p.stdout:
    r = {}
    m = re.match(r'^(.+)([0-9a-z]{40})\s*\(([^\)]+)\)(\S+)', line)
    assert m, "<%s>" % line.rstrip()
    name,h,refs,email = m.groups()

    for ref in refs.split(', '):
        if ref=="master":
            master = h
        elif ref=="test/master":
            # Override upstream_commit for testing
            upstream_commit = "test/master"
        elif re_match(r'^reverts/r(\d+)', ref, r):
            svnrev = int(r["m"].group(1))
            print("\tBranch: %s" % reverts.refspec(svnrev))
            reverted[svnrev] = (name, email)
        elif re_match(r'^recommits/r(\d+)', ref, r):
            svnrev = int(r["m"].group(1))
            print("\tBranch: %s" % reverts.refspec_m(svnrev))
            recommitted[svnrev] = (name, email)
        elif re.match(r'^dev/('+'|'.join(generated_branches)+')', ref):
            pass
        elif re_match(r'^staged/(\S+)\.r(\d+)', ref, r):
            topic_svnrev = int(r["m"].group(2))
            topics = staged_topics.get(topic_svnrev, [])
            topics.append(r["m"].group(1))
            staged_topics[topic_svnrev] = topics
            print("\tTopics(r%d): %s" % (topic_svnrev, topics[-1]))
        elif re_match(r'dev/(\S+)', ref, r):
            unstaged_topics.append(r["m"].group(1))
            print("\tTopics: %s" % unstaged_topics[-1])

p.wait()

for svnrev in reverted.keys():
    if  svnrev in recommitted:
        reverts.register(svnrev, recommitted[svnrev][0], recommitted[svnrev][1])
    else:
        reverts.register(svnrev, reverted[svnrev][0], reverted[svnrev][1])

assert master is not None

# Make sure we are alywas on detached head.
run_cmd(["git", "checkout", "-qf", master])

# Seek culprit rev, rewind and revert
invalidated_ssid = None
if culprit_svnrev is not None:
    svn_commit = git_merge_base(first_ss["project"], upstream_commit)

    revert_ref = reverts.refspec(culprit_svnrev)

    # Confirm if the revert exists.
    if eval_cmd(["git", "rev-parse", "--verify", "-q", revert_ref]):
        print("%s exists. Do nothing." % revert_ref)
    else:
        # Calculate range(ssid) to invalidate previous builds
        assert first_ss is not None
        # Get the latest ss.
        resp = urlopen(api_url+'sourcestamps?limit=1&order=-ssid')
        sourcestamps = json.load(resp)
        resp.close()
        # FIXME: Assumes ssid equal chid.
        invalidated_ssid = "%d..%d" % (first_ss["ssid"], sourcestamps["sourcestamps"][0]["ssid"])

        # Rewind master to one commit before the revertion.
        master = "%s^" % first_ss["project"]
        run_cmd(["git", "branch", "-f", "master", master])

        for svnrev in sorted(culprit_svnrevs.keys()):
            ss = culprit_svnrevs[svnrev]
            head = ss["project"]
            svn_commit = git_merge_base(head, upstream_commit)
            print("head=%s master=%s svn=%s" % (head, master, svn_commit))
            revert_h = reverts.revert(svn_commit, svnrev, head)
elif first_ss is not None:
    # Doesn't revert. Just skip.
    svn_commit = git_merge_base(first_ss["project"], upstream_commit)

    # Confirm if the revert exists.
    if False:
        print("%s exists. Do nothing." % revert_ref)
    else:
        # Calculate range(ssid) to invalidate previous builds
        # Get the latest ss.
        resp = urlopen(api_url+'sourcestamps?limit=1&order=-ssid')
        sourcestamps = json.load(resp)
        resp.close()
        # FIXME: Assumes ssid equal chid.
        invalidated_ssid = "%d..%d" % (first_ss["ssid"], sourcestamps["sourcestamps"][0]["ssid"])

        # Rewind master to one commit before the revertion.
        master = "%s^" % first_ss["project"]
        print("master=%s svn=%s" % (master, svn_commit))

        run_cmd(["git", "branch", "-f", "master", master])
else:
    # FIXME: Seek diversion of upstream
    pass

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

    m = re.match(r'^(.+)\s<([^>]*)>$', commit["author"])
    author_name = m.group(1)
    author_email = m.group(2)

    # FIXME: Invalidate ssid with api.
    if invalidated_ssid is not None:
        props["invalidated_changes"] = invalidated_ssid

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

        reverts.remove(revert_svnrev)

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
        for revert_svnrev in reverts:
            if revert_svnrev > svnrev:
                continue
            local_reverts.append(reverts.refspec(revert_svnrev))
        assert local_reverts
        if do_merge(local_reverts, ff=False, name=author_name, email=author_email):
            print("\trevert: Applied %s" % str(local_reverts))
            commit["files"]=set()
            head = git_head() # Don't update master here.

            # Make recommits
            reverts.make_recommit(svn_commit, svnrev, head, name=author_name, email=author_email)
            git_reset(head)
        else:
            print("\trevert: Local reverts failed. %s" % str(local_reverts))
            git_reset(master)
            reverts.remove(svnrev)

    # Apply svn HEAD
    if graduated:
        print("\tgrad: Applying graduated commit: %s" % graduated)
        assert do_merge(graduated, ff=False)
    elif not local_reverts:
        print("\tApplying r%d..." % svnrev)

        if do_merge([svn_commit], ff=True, msg="Merged r%d" % svnrev, stdout=True, name=author_name, email=author_email):

            # if files are present but commit is empty, check graduation.
            if commit["files"]:
                head = git_head()
                reverts.check_graduated(svn_commit)
                git_reset(head)

            # Check waiting recommits by author.
            interests = []
            for ref,names,emails in reverts.gen_recommits(svnrev, want_tuple=True):
                if author_name in names or author_email in emails:
                    interests.append(ref)
            if commit["files"] and interests:
                chain_recommit = interests
        else:
            # Rather than chain-revert, attempt to commit.
            interested_names = set([author_name])
            interested_emails = set([author_email])

            # At first, make the least set of cands.
            cands_set = set(attempt_merge(svn_commit, list(reverts.gen_recommits(svnrev))))

            # Pick up interested users from cands
            for ref,names,emails in reverts.gen_recommits(svnrev, want_tuple=True):
                if ref in cands_set:
                    interested_names |= names
                    interested_emails |= emails

            # Generate interested_set
            cands_set |= set([t[0] for t in reverts.gen_recommits(svnrev, want_tuple=True, names=interested_names, emails=interested_emails)])

            # Regenerate cands
            cands = []
            for cand in reverts.gen_recommits(svnrev):
                if cand in cands_set:
                    cands.append(cand)

            git_reset(master)
            if do_merge(cands + [svn_commit], stdout=True):
                print("\tApplied r%d with %s" % (svnrev, str(cands)))
                # FIXME: Mark proerty as it is synthesized
            else:
                # Chain revert
                revert_h = reverts.revert(svn_commit, svnrev, master)
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
        assert do_merge(chain_recommit, stdout=True, name=author_name, email=author_email, msg=msg)
        print("\tRecommit for %s: %s" % (author_name, str(chain_recommit)))

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
        if invalidated_ssid is not None:
            commit["properties"]["invalidated_changes"] = invalidated_ssid

        commit["files"]=git_diff_files("master")
        if commit["files"]:
            commit["properties"]=json.dumps(commit["properties"])
            commit["files"]=json.dumps(sorted(commit["files"]))

            master = git_head()
            if post_commit(commit):
                print("\tRecommit for %s: done." % author_name)
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
            p = subprocess.Popen(
                [
                    "git", "log",
                    "--no-walk",
                    "--format=raw",
                    "--stat=1024,1000",
                    staged_ref,
                    ],
                stdout=subprocess.PIPE,
                )
            commits = list(collect_commits(p.stdout))
            assert p.wait() == 0
            assert len(commits)==1
            commit = commits[0]

            # FIXME: Invalidate ssid with api.
            if invalidated_ssid is not None:
                commit["properties"]["invalidated_changes"] = invalidated_ssid

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
    p = subprocess.Popen(
        [
            "git", "log",
            "--no-walk",
            "--format=raw", "--show-notes",
            "--stat=1024,1000",
            upstream_commit,
            ],
        stdout=subprocess.PIPE,
        )
    commits = list(collect_commits(p.stdout))
    assert p.wait() == 0
    assert len(commits)==1
    commit = commits[0]
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
    p = subprocess.Popen(
        [
            "git", "log",
            "--no-walk",
            "--format=raw",
            "--stat=1024,1000",
            topic_ref,
        ],
        stdout=subprocess.PIPE,
        )
    commits = list(collect_commits(p.stdout))
    assert p.wait() == 0
    assert len(commits)==1
    commit = commits[0]

    # FIXME: Invalidate ssid with api.
    if invalidated_ssid is not None:
        commit["properties"]["invalidated_changes"] = invalidated_ssid

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
