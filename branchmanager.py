import json
import re
import subprocess

from cmds import *

class BranchManager:
    def __init__(self, generated=[], ignored=[]):
        self._branches = {}
        self._pushed_refs = None

        for ref in generated:
            m = re.match(r'^([^/]+)/$', ref)
            if m:
                self._branches[m.group(1)] = {}

        # Retrieve all branches
        p = subprocess.Popen(
            [
                "git", "log",
                "--no-walk",
                "--branches", "--remotes=dev",
                "--format=%B%aN%H%d%aE",
                ],
            stdout=subprocess.PIPE,
            )

        body = []
        for line in p.stdout:
            r = {}
            m = re.match(r'^(.+)([0-9a-z]{40})\s*\(([^\)]+)\)(\S+)', line)
            if not m:
                body.append(line)
                continue
            assert m, "<%s>" % line.rstrip()
            name,h,refs,email = m.groups()

            bba = None
            if len(body) >= 4 and body[2] == "{\n":
                bba = json.loads(''.join(body[2:]))

            for ref in refs.split(', '):
                if re.match(r'^dev/('+'|'.join(generated + ignored)+')', ref):
                    pass
                rs = ref.split('/')
                rr = rs.pop()
                d = self._branches
                for r in rs:
                    if r not in d:
                        d[r] = {}
                    d = d[r]
                d[rr] = dict(
                    name=name,
                    email=email,
                    h=h,
                    )
                if bba is not None:
                    d[rr]["bba"] = bba
            body = []

        p.wait()

    # Accessors
    def __contains__(self, i):
        return i in self._branches

    def __getitem__(self, i):
        item = self._branches
        for rr in i.split('/'):
            if rr not in item:
                item[rr] = {}
            item = item[rr]
        return item

    def keys(self):
        return self._branches.keys()

    # Git-push services
    def push_refs(self):
        if self._pushed_refs is None:
            return
        run_cmd(["git", "push", "dev"] + list(self._pushed_refs))
        self._pushed_refs = None

    def push_later(self):
        if self._pushed_refs is None:
            self._pushed_refs = set()

    def push_ref(self, ref):
        if self._pushed_refs is None:
            run_cmd(["git", "push", "dev", ref])
        else:
            self._pushed_refs.add(ref)

    # refs methods
    @staticmethod
    def revert_ref(svnrev):
        return "reverts/r%d" % svnrev

    @staticmethod
    def recommit_ref(svnrev):
        return "recommits/r%d" % svnrev

    def revert(self, svnrev, commit, bba=None):
        ref = self.revert_ref(svnrev)
        run_cmd(["git", "branch", "-f", ref, commit])
        if bba:
            print(self[ref])
            self[ref]["bba"] = bba

    # Basic ref methods
    def remove(self, ref):
        run_cmd(["git", "branch", "-D", ref])
        self.push_ref(":%s" % ref)

        rs = ref.split('/')
        tail = rs.pop()
        item = self.branches
        for rr in rs:
            item = item[rr]
        del item[tail]

    # They can be removed later.
    def may_graduate(self, svnrev, revert_svnrev):
        revert_ref = self.recommit_ref(revert_svnrev)
        grad_ref = "graduates/r%d/r%d" % (revert_svnrev, svnrev)
        run_cmd(["git", "branch", "-f", grad_ref, revert_ref], stdout=True)
        self._branches["graduates"]["r%d" % revert_svnrev] = {"r%d" % svnrev: {}}

    def graduate(self, svnrev, revert_svnrev):
        recommit_ref = self.recommit_ref(revert_svnrev)
        grad_ref = "graduates/r%d/r%d" % (revert_svnrev, svnrev)
        run_cmd(["git", "branch", "-M", recommit_ref, grad_ref], stdout=True)
        eval_cmd(["git", "branch", "-D", self.revert_ref(revert_svnrev)], stdout=True)
        self._branches["graduates"]["r%d" % revert_svnrev] = {"r%d" % svnrev: {}}
