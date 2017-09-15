#!/usr/bin/python

import json
import re
import subprocess
import sys

from urllib import *

git_dir = '/home/chapuni/bb-automaton/llvm-project'
bb_url = 'http://aws-ubu.pgr.jp:8010/'
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

# Create revert object.
def revert(h):
    r = subprocess.Popen(["git", "reset", "-q", '--hard', h]).wait()
    assert r == 0

    p = subprocess.Popen(
        ["git", "revert", "--no-edit", h],
        stdout=subprocess.PIPE,
        )
    line = ''.join(p.stdout.readlines())
    m = re.match(r'\[detached HEAD\s+([0-9a-f]+)\]', line)
    assert m, "git-revert ====\n%s====" % line
    p.wait()
    return m.group(1)

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
                "files": [],
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
            commit["files"].append(r["m"].group(1))
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
            if culprit_svnrev is None or culprit_svnrev > svnrev:
                culprit_svnrev = svnrev
                first_ss = ss

# Git

p = subprocess.Popen(
    ["git", "branch", "-v"],
    stdout=subprocess.PIPE,
    )

revert_svnrevs = []
master = None

# git-branch is sorted
for line in p.stdout:
    r={}
    if re_match(r'^.\s+reverts/r(\d+)', line, r):
        svnrev = int(r["m"].group(1))
        print("reverts/r%d" % svnrev)
        revert_svnrevs.append(svnrev)
    elif re_match(r'^.\s+test/master', line, r):
        # Override upstream_commit for testing
        upstream_commit = "test/master"
    elif re_match(r'^.\s+master\s+([0-9a-f]+)', line, r):
        master = r["m"].group(1)

p.wait()

assert master is not None

# Make sure we are alywas on detached head.
r = subprocess.Popen(["git", "checkout", "-qf", master]).wait()
assert r == 0

revert_svnrevs = list(reversed(sorted(revert_svnrevs)))

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

    revert_ref = "reverts/r%d" % culprit_svnrev

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
        invalidated_ssid = "%d..%d" % (first_ss["ssid"], sourcestamps["sourcestamps"][0]["ssid"])

        # Rewind master to one commit before the revertion.
        master = "%s^" % first_ss["project"]
        r = subprocess.Popen(["git", "branch", "-f", "master", master]).wait()
        assert r == 0

        revert_h = revert(svn_commit)
        revert_svnrevs.insert(0, culprit_svnrev)

        # Register the revert
        r = subprocess.Popen(["git", "branch", "-f", revert_ref, revert_h]).wait()
        print("Reverted %s (invalidate %s)" % (revert_ref, invalidated_ssid))
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

    commit["files"]=json.dumps(commit["files"])

    # FIXME: Invalidate ssid with api.
    if invalidated_ssid is not None:
        props["invalidated_ssid"] = invalidated_ssid

    print("========Processing r%d" % svnrev)

    # Check graduation
    # FIXME: Skip if change is nothing to do.
    graduated = []
    for revert_svnrev in list(revert_svnrevs):
        revert_ref = "reverts/r%d" % revert_svnrev
        print("\tgrad: Checking %s" % revert_ref)
        subprocess.Popen(["git", "reset", "-q", "--hard", svn_commit]).wait()
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
        commit["files"]=json.dumps([]) # FIXME: Would it be partial?

        r = subprocess.Popen(["git", "branch", "-D", revert_ref]).wait()
        assert r == 0
        revert_svnrevs.remove(revert_svnrev)

    subprocess.Popen(["git", "reset", "-q", "--hard", master]).wait()

    # Apply reverts
    local_reverts = []
    if svnrev in revert_svnrevs:
        print("\trevert: Checking r%d" % svnrev)
        for revert_svnrev in revert_svnrevs:
            if revert_svnrev > svnrev:
                continue
            local_reverts.append("reverts/r%d" % revert_svnrev)
        assert local_reverts
        p = subprocess.Popen(
            ["git", "merge", "--no-ff"] + local_reverts,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            )
        o = ''.join(p.stdout.readlines())
        e = ''.join(p.stderr.readlines())
        assert p.wait() == 0, "o<%s>\ne<%s>" % (o, e)
        print("\trevert: Applied %s" % str(local_reverts))
        commit["files"]=json.dumps([])
        # Note: master is unknown here!

    # Apply svn HEAD
    if graduated:
        print("\tgrad: Applying graduated commit: %s" % graduated)
        p = subprocess.Popen(
            ["git", "merge", "--no-ff"] + graduated,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            )
        o = ''.join(p.stdout.readlines())
        e = ''.join(p.stderr.readlines())
        assert p.wait() == 0, "o<%s>\ne<%s>" % (o, e)
    elif not local_reverts:
        print("\tApplying r%d..." % svnrev)

        # FIXME: Add svnrev
        r = subprocess.Popen(["git", "merge", svn_commit]).wait()

        if r != 0:
            # Chain revert
            revert_h = revert(svn_commit)
            commit["files"]=json.dumps([])
            revert_ref = "reverts/r%d" % svnrev
            r = subprocess.Popen(["git", "branch", "-f", revert_ref, revert_h]).wait()
            assert r == 0
            revert_svnrevs.insert(0, svnrev)
            r = subprocess.Popen(["git", "reset", "-q", "--hard", master]).wait()
            assert r == 0
            # FIXME: Add message
            p = subprocess.Popen(
                ["git", "merge", revert_ref],
                    stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                )
            o = ''.join(p.stdout.readlines())
            e = ''.join(p.stderr.readlines())
            assert p.wait() == 0, "o<%s>\ne<%s>" % (o, e)
            print("\tApplied new %s" % revert_ref)

    p = subprocess.Popen(
        ["git", "rev-parse", "HEAD"],
        stdout=subprocess.PIPE,
        )
    m = re.match(r'^([0-9a-f]{40})', p.stdout.readline())
    assert m
    master = m.group(1)
    p.wait()

    props["commit"] = master
    commit["properties"]=json.dumps(props)

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
        r = subprocess.Popen(["git", "branch", "-f", "master", master]).wait()
        assert r == 0
    else:
        print("Dry run -- r%d" % svnrev)

#EOF
