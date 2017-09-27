import subprocess

from cmds import *
from gitutil import *

# Revert controller
class RevertManager:
    def __init__(self, branches):
        self._branches = branches

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

    def graduate(self, svnrev):
        self._svnrevs.remove(svnrev)
        self._branches.graduate(svnrev)

    # def remove(self, svnrev):
    #     self._svnrevs.remove(svnrev)
    #     run_cmd(["git", "branch", "-D", self.refspec(svnrev), self.refspec_m(svnrev)], stdout=True)

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
            env=env,
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

    def gen_recommits_cands(self, svn_commit, svnrev, name, email):
        cands = []

        # Rather than chain-revert, attempt to commit.
        interested_names = set([name])
        interested_emails = set([email])

        # At first, make the least set of cands.
        cands_set = set(attempt_merge(svn_commit, list(self.gen_recommits(svnrev))))

        # Pick up interested users from cands
        for ref,names,emails in self.gen_recommits(svnrev, want_tuple=True):
            if ref in cands_set:
                interested_names |= names
                interested_emails |= emails

        # Generate interested_set
        cands_set |= set([t[0] for t in self.gen_recommits(svnrev, want_tuple=True, names=interested_names, emails=interested_emails)])

        # Regenerate cands
        for cand in self.gen_recommits(svnrev):
            if cand in cands_set:
                cands.append(cand)

        return cands

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
        print("\tMake-recommit r%d: candidates %s" % (svnrev,str(recommit_cand)))

        recommit_cand = attempt_merge(recommit_h, recommit_cand)

        # Create the actual recommit on the revert.
        git_reset(self.refspec(svnrev))
        msg = "Merge %s" % recommit_ref
        if recommit_cand:
            msg += " with " + ", ".join(recommit_cand)
        recommit_cand.append(recommit_h)
        print("\tMake-recommit r%d: %s" % (svnrev, msg))
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
            self._branches.graduate(svnrev)
            #run_cmd(["git", "branch", "-D", revert_ref, self.refspec_m(svnrev)], stdout=True)
