import os
import subprocess
import sys

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
