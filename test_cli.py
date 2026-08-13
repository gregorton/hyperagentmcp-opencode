"""Checks for normalize_argv: a bare invocation defaults to the `run` subcommand."""
from hyperagent_code import normalize_argv

failures = []


def check(name, cond):
    if cond:
        print("  PASS  " + name)
    else:
        print("  FAIL  " + name)
        failures.append(name)


check("[] -> ['run']", normalize_argv([]) == ["run"])
check("['setup'] unchanged", normalize_argv(["setup"]) == ["setup"])
check("['setup', '--small-model', 'x'] unchanged",
      normalize_argv(["setup", "--small-model", "x"]) == ["setup", "--small-model", "x"])
check("['serve'] unchanged", normalize_argv(["serve"]) == ["serve"])
check("['run', '--continue'] unchanged",
      normalize_argv(["run", "--continue"]) == ["run", "--continue"])
check("['--continue'] -> ['run', '--continue']",
      normalize_argv(["--continue"]) == ["run", "--continue"])
check("['myfile.py'] -> ['run', 'myfile.py']",
      normalize_argv(["myfile.py"]) == ["run", "myfile.py"])
check("['--debug'] -> ['--debug', 'run']",
      normalize_argv(["--debug"]) == ["--debug", "run"])
check("['--debug', 'setup'] -> ['--debug', 'setup']",
      normalize_argv(["--debug", "setup"]) == ["--debug", "setup"])
check("['--debug', '--continue'] -> ['--debug', 'run', '--continue']",
      normalize_argv(["--debug", "--continue"]) == ["--debug", "run", "--continue"])
check("['-h'] unchanged", normalize_argv(["-h"]) == ["-h"])
check("['--help'] unchanged", normalize_argv(["--help"]) == ["--help"])

if failures:
    raise SystemExit(1)
print("ALL CLI CHECKS PASSED")
