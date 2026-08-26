# Test Convention

Use the Python standard-library unittest runner unless a concrete test need justifies another tool.

Tests cover only implemented v0.1 behavior: package and CLI smoke checks, Profile / Task Packet contracts, loopback API boundaries, system TLS context, Git worktree isolation, and the TERM → confirmed KILL process path. Do not add scaffold tests for unimplemented routing, accounting, or provider fallbacks.

From the project development environment, run:

~~~bash
.venv/bin/python -m unittest discover -s tests -v
~~~
