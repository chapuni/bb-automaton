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
        return self._branches[i]

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

    # Basic ref methods
    def remove(self, ref):
        run_cmd(["git", "branch", "-D", ref])
        self.push_ref(":%s" % ref)

    # They can be removed later.
    def may_graduate(self, svnrev):
        ref = self.recommit_ref(svnrev)
        run_cmd(["git", "branch", "-f", "graduates/r%d" % svnrev, ref], stdout=True)

    def graduate(self, svnrev):
        ref = self.recommit_ref(svnrev)
        run_cmd(["git", "branch", "-M", ref, "graduates/r%d" % svnrev], stdout=True)
        eval_cmd(["git", "branch", "-D", ref, self.revert_ref(svnrev)], stdout=True)
