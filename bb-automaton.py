#!/usr/bin/python

import json
import re
import subprocess
import sys

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

def get_recentbuilds(builderid=None, limit=24):
    q = {
        "order": "-buildid",
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
    good = None
    bad = None
    for i,brd in enumerate(recentbuilds[builderid]):
        if brd["results"] == 2:
            bad = i
        if brd["results"] in (0, 1):
            good = i
            break
    if good is None:
        # Retrieve
        recentbuilds[builderid] = get_recentbuilds(builderid, limit=256)[builderid]
        for i,brd in enumerate(recentbuilds[builderid]):
            if brd["results"] == 2:
                bad = i
            if brd["results"] in (0, 1):
                good = i
                break

    if good is None:
        print("warning: good is none")
        return None

    # Seek bad builds from the oldest one.
    builds = reversed(recentbuilds[builderid][0:bad+1])
    culprit_ss = None
    for i,brd in enumerate(builds):
        result = brd.get("results", -1)
        if result is None:
            result = -1

        if result == 2:
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
                    if i > 0 and bset["reason"] != "bisect":
                        continue
                    print("len(ss)=%d reason=<%s>" % (len(bset["sourcestamps"]), bset["reason"]))
                    for ss in bset["sourcestamps"]:
                        if ss["revision"] not in revs:
                            first_ss = ss
                            revs.append(ss["revision"])
            print(revs)
            if len(revs)==1:
                assert first_ss is not None
                assert revs[0] == first_ss["revision"]
                culprit_ss = first_ss
                break

    if culprit_ss is not None:
        print("Culprit is %s (ssid=%d)" % (culprit_ss["revision"], culprit_ss["ssid"]))

    return culprit_ss

# Oneliner expects success.
def run_cmd(args):
    p = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        )
    o = ''.join(p.stdout.readlines())
    e = ''.join(p.stderr.readlines())
    assert p.wait() == 0, "o<%s>\ne<%s>" % (o, e)

def git_reset(head="HEAD"):
    run_cmd(["git", "reset", "-q", '--hard', head])

# Oneliner expects success.
def git_head():
    p = subprocess.Popen(
        ["git", "rev-parse", "HEAD"],
        stdout=subprocess.PIPE,
        )
    m = re.match(r'^([0-9a-f]{40})', p.stdout.readline())
    assert m
    p.wait()
    return m.group(1)

# Create revert object.
def revert(h, msg=None):
    git_reset(h)

    if msg:
        run_cmd(["git", "revert", "--no-commit", h])
        cmdline = ["git", "commit", "-m", msg]
    else:
        cmdline = ["git", "revert", "--no-edit", h]

    p = subprocess.Popen(
        cmdline,
        stdout=subprocess.PIPE,
        )
    line = ''.join(p.stdout.readlines())
    m = re.match(r'\[detached HEAD\s+([0-9a-f]+)\]', line)
    assert m, "git-revert ====\n%s====" % line
    p.wait()

    return m.group(1)

# Revert controller
class RevertController:
    def __init__(self):
        # Order by reverse revs
        self._svnrevs = []

        # rev:set() changed files
        self._changes = {}

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

    def changes(self, svnrev):
        assert svnrev in self._svnrevs
        if svnrev in self._changes:
            return self._changes[svnrev]

        refspec = self.refspec(svnrev)

        p = subprocess.Popen(
            ["git", "diff", "--name-only", refspec, "%s^" % refspec],
            stdout=subprocess.PIPE,
            )

        changes = set()
        for line in p.stdout:
            changes.add(line.rstrip())

        self._changes[svnrev] = changes
        return changes

    def register(self, svnrev):
        if svnrev in self._svnrevs:
            self._svnrevs.remove(svnrev)
        self._svnrevs.append(svnrev)
        # FIXME: Do smarter!
        self._svnrevs = list(reversed(sorted(self._svnrevs)))

    def remove(self, svnrev):
        self._svnrevs.remove(svnrev)
        r = subprocess.Popen(["git", "branch", "-D", self.refspec(svnrev)]).wait()
        assert r == 0

    # This moves HEAD
    def revert(self, h, svnrev):
        revert_h = revert(h, "Revert r%d" % svnrev)
        self._svnrevs.insert(0, svnrev)
        assert len(self._svnrevs) == 1 or self._svnrevs[0] > self._svnrevs[1]

        revert_ref = "reverts/r%d" % svnrev
        run_cmd(["git", "branch", "-f", revert_ref, revert_h])
        print("\t*** Revert %s" % revert_ref)

        # Make recommit
        p = subprocess.Popen(
            ["git", "commit", "-m", "Recommit r%d" % svnrev],
            stdout=subprocess.PIPE,
            )
        line = ''.join(p.stdout.readlines())
        m = re.match(r'\[detached HEAD\s+([0-9a-f]+)\]', line)
        assert m, "git-recommit ====\n%s====" % line
        p.wait()
        recommit_h = m.group(1)

        recommit_ref = "recommits/r%d" % svnrev
        r = subprocess.Popen(["git", "branch", "-f", recommit_ref, recommit_h]).wait()

        return revert_h

# Collect commits from git-svn
def collect_commits(master, upstream):
    p = subprocess.Popen(
        [
            "git", "log",
            "--reverse",
            "--format=%H%n%B%aN:%aE:%at\n%N",
            "--stat=1024,1000",
            master+".."+upstream,
        ],
        stdout=subprocess.PIPE,
        )

    commit=None
    while True:
        line = p.stdout.readline()
        r={}
        if line=="":
            break
        if re_match(r'^([0-9a-f]{40})', line, r):
            if commit is not None:
                yield commit
            commit={
                "commit": r["m"].group(1),
                "comments": "",
                "files": set(),
                "project": "",
                "branch": "master",
                "repository": "",
                "category": "",
                "codebase": "",
                }
            # Body, ends with authors
            while True:
                line=p.stdout.readline()
                assert line != ""
                if re_match(r'^([^:]+):([^:]+):(\d+)$', line.rstrip(), r):
                    break
                commit["comments"] += line
            m=r["m"]
            commit["author"] = "%s <%s>" % (m.group(1), m.group(2))
            commit["when"] = int(m.group(3))

            line=p.stdout.readline()
            if re_match(r'^git-svn-rev:\s*(\d+)', line, r):
                commit["revision"]="r"+r["m"].group(1)
                commit["revlink"]="https://reviews.llvm.org/rL"+r["m"].group(1)
            else:
                assert line=="\n"
        elif re_match("^\s+(\w[^|]+[^ |])\s+\|", line, r):
            # Seek stat
            commit["files"].add(r["m"].group(1))
        else:
            # Possibly garbage in file status
            pass

    if commit is not None:
        yield commit

    p.wait()

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
            if culprit_svnrev is None or culprit_svnrev > svnrev:
                culprit_svnrev = svnrev
                first_ss = ss

# Git

p = subprocess.Popen(
    ["git", "branch", "-v"],
    stdout=subprocess.PIPE,
    )

reverts = RevertController()
master = None

# git-branch is sorted
for line in p.stdout:
    r={}
    if re_match(r'^.\s+reverts/r(\d+)', line, r):
        svnrev = int(r["m"].group(1))
        print("reverts/r%d" % svnrev)
        reverts.register(svnrev)
    elif re_match(r'^.\s+test/master', line, r):
        # Override upstream_commit for testing
        upstream_commit = "test/master"
    elif re_match(r'^.\s+master\s+([0-9a-f]+)', line, r):
        master = r["m"].group(1)

p.wait()

assert master is not None

# Make sure we are alywas on detached head.
run_cmd(["git", "checkout", "-qf", master])

# Seek culprit rev, rewind and revert
invalidated_ssid = None
if culprit_svnrev is not None:
    p = subprocess.Popen(
        ["git", "merge-base", first_ss["project"], upstream_commit],
        stdout=subprocess.PIPE,
        )
    m = re.match(r'^([0-9a-f]{40})', p.stdout.readline())
    assert m
    svn_commit = m.group(1)
    p.wait()

    revert_ref = reverts.refspec(culprit_svnrev)

    # Confirm if the revert exists.
    p = subprocess.Popen(
        ["git", "rev-parse", "--verify", "-q", revert_ref],
        stdout=subprocess.PIPE,
        )
    p.stdout.readlines() # Discard stdout
    r = p.wait()

    if r == 0:
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
            p = subprocess.Popen(
                ["git", "merge-base", head, upstream_commit],
                stdout=subprocess.PIPE,
                )
            m = re.match(r'^([0-9a-f]{40})', p.stdout.readline())
            assert m
            svn_commit = m.group(1)
            p.wait()
            revert_h = reverts.revert(svn_commit, svnrev, head)
else:
    # FIXME: Seek diversion of upstream
    pass

for commit in collect_commits("master", upstream_commit):
    svn_commit = commit["commit"]
    m = re.match('^r(\d+)', commit["revision"]) # rNNNNNN
    assert m
    svnrev = int(m.group(1))
    props={
        "commit": svn_commit,
        }
    del commit["commit"]

    # FIXME: Invalidate ssid with api.
    if invalidated_ssid is not None:
        props["invalidated_changes"] = invalidated_ssid

    print("========Processing r%d" % svnrev)

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
        p = subprocess.Popen(
            ["git", "merge", "--squash", revert_ref],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            )
        p.stdout.readlines() # Discard
        p.stderr.readlines() # Discard
        r = p.wait()
        if r != 0:
            continue
        p = subprocess.Popen(
            ["git", "diff", "--exit-code", "--shortstat", "--cached"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            )
        p.stdout.readlines() # Discard
        p.stderr.readlines() # Discard
        r = p.wait()
        if r != 0:
            continue
        # Merge isn't affected. Assume graduated.
        print("\tgrad: %s is graduated." % revert_ref)

        # Make "Revert Revert" from svn_commit.
        # Anyways, I cannot revert reverts/rXXXXXX.
        graduated.append(revert(svn_commit))
        commit["files"]=set()

        reverts.remove(revert_svnrev)

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
        run_cmd(["git", "merge", "--no-ff"] + local_reverts)
        print("\trevert: Applied %s" % str(local_reverts))
        commit["files"]=set()
        # Note: master is unknown here!

    # Apply svn HEAD
    if graduated:
        print("\tgrad: Applying graduated commit: %s" % graduated)
        run_cmd(["git", "merge", "--no-ff"] + graduated)
    elif not local_reverts:
        print("\tApplying r%d..." % svnrev)

        r = subprocess.Popen(["git", "merge", "-m", "Merged r%d" % svnrev, svn_commit]).wait()

        if r != 0:
            # Chain revert
            revert_h = reverts.revert(svn_commit, svnrev)
            commit["files"]=set()
            revert_ref = reverts.refspec(svnrev)
            git_reset(master)
            # FIXME: Add message
            run_cmd(["git", "merge", revert_ref])
            print("\tApplied new %s" % revert_ref)
            master = git_head()

    master = git_head()

    props["commit"] = master
    commit["properties"]=json.dumps(props)
    commit["files"]=json.dumps(sorted(commit["files"]))

    # XXX Hack
    commit["project"] = master

    # Post the commit
    if True:
        resp=urlopen(change_url, urlencode(commit))
        for line in resp:
            print(line.rstrip())
        st=resp.getcode()
        if st != 200:
            print("status=%d" % resp.getcode())
            break
        resp.close()
        run_cmd(["git", "branch", "-f", "master", master])
    else:
        print("Dry run -- r%d" % svnrev)

#EOF
