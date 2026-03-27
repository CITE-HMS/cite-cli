# cite-cli

[![License](https://img.shields.io/pypi/l/cite-cli.svg?color=green)](https://github.com/CITE-HMS/cite-cli/raw/main/LICENSE)
[![PyPI](https://img.shields.io/pypi/v/cite-cli.svg?color=green)](https://pypi.org/project/cite-cli)
[![Python Version](https://img.shields.io/pypi/pyversions/cite-cli.svg?color=green)](https://python.org)
[![CI](https://github.com/CITE-HMS/cite-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/CITE-HMS/cite-cli/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/CITE-HMS/cite-cli/branch/main/graph/badge.svg)](https://codecov.io/gh/CITE-HMS/cite-cli)

command line tools for CITE@HMS

In Task Scheduler, create a new task and set the action to `Start a program` with the following command:
- Program/script: `C:\Windows\System32\cmd.exe`
- Add arguments (optional): `/c "C:\Users\User\.local\bin\uv.exe tool run --from git+https://github.com/CITE-HMS/cite-cli cite clean -d 25 -f > C:\cite_cli_log.log 2>&1"`

