import re
import subprocess
import sys

from cmds import *

def re_match(expr, line, r):
    m = re.match(expr, line)
    r["m"] = m
    return m

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
    head = git_head()
    i = 0
    while cands and i < len(cands):
        target_rev = cands[i]
        cand = cands[:]
        cand.pop(i)
        git_reset(head)
        r = do_merge(cand + [commit], commit=False)
        if r:
            print("\tRecommit: <%s>: Removing should be safe." % target_rev)
            cands.pop(i)
        else:
            # Failed. Try next.
            print("\tRecommit: <%s>: It was essential. " % target_rev)
            i += 1

    return cands

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
            if re_match("^\s+(\S[^|]+[^ |])\s+\|", line, r):
                while re_match("^\s+(\w[^|]+[^ |])\s+\|", line, r):
                    commit["files"].add(r["m"].group(1))
                    line = fh.readline()
                    continue
            if re.match(r"^\s*\d+\sfiles\schanged,", line):
                # Discard one line.
                # NN files changed, NN insertions(+), NN deletions(-)
                line = fh.readline()
            if line=="":
                continue
            assert line == "\n", "<%s>%s" % (line,commit["commit"])
            line = fh.readline()
            state="commit"
            continue
        assert False, "<%s>" % line

def collect_single_commit(commit):
    p = subprocess.Popen(
        [
            "git", "log",
            "--no-walk",
            "--format=raw",
            "--show-notes",
            "--stat=1024,1000",
            commit,
            ],
        stdout=subprocess.PIPE,
        )
    commits = list(collect_commits(p.stdout))
    assert p.wait() == 0
    assert len(commits)==1
    return commits[0]
