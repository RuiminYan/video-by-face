@echo off
pushd "%~dp0"
uv run python -X utf8 fetch_competition.py %*
popd
