#!/bin/bash
set -euo pipefail
cd /tmp
echo "== python"; python -c 'import sys, sysconfig; print(sys.version.split()[0], sysconfig.get_paths()["include"])'
echo "== agent CLIs"; claude --version; codex --version
echo "== uv sync with compiled dependencies"
mkdir -p /tmp/proj/cext/src && cd /tmp/proj
cat > cext/pyproject.toml <<'P'
[build-system]
requires = ["setuptools>=70"]
build-backend = "setuptools.build_meta"
[project]
name = "cext"
version = "0.1.0"
[tool.setuptools]
ext-modules = [{name = "cext", sources = ["src/cext.c"]}]
P
cat > cext/src/cext.c <<'C'
#include <Python.h>
static PyObject *add(PyObject *s, PyObject *a) { long x, y; if (!PyArg_ParseTuple(a, "ll", &x, &y)) return NULL; return PyLong_FromLong(x + y); }
static PyMethodDef M[] = {{"add", add, METH_VARARGS, ""}, {NULL, NULL, 0, NULL}};
static struct PyModuleDef D = {PyModuleDef_HEAD_INIT, "cext", NULL, -1, M};
PyMODINIT_FUNC PyInit_cext(void) { return PyModule_Create(&D); }
C
mkdir -p skb/src
cat > skb/pyproject.toml <<'P'
[build-system]
requires = ["scikit-build-core>=0.10"]
build-backend = "scikit_build_core.build"
[project]
name = "skb"
version = "0.1.0"
P
cat > skb/CMakeLists.txt <<'C'
cmake_minimum_required(VERSION 3.15)
project(skb LANGUAGES C)
find_package(Python COMPONENTS Interpreter Development.Module REQUIRED)
Python_add_library(skb MODULE src/skb.c WITH_SOABI)
install(TARGETS skb DESTINATION .)
C
sed 's/cext/skb/g' cext/src/cext.c > skb/src/skb.c
cat > pyproject.toml <<'P'
[project]
name = "app"
version = "0.1.0"
requires-python = ">=3.14"
dependencies = ["cext", "skb", "markupsafe==3.0.3"]
[tool.uv.sources]
cext = { path = "cext" }
skb = { path = "skb" }
[tool.uv]
no-binary-package = ["markupsafe"]
P
command -v cmake && { echo "unexpected: system cmake present"; exit 1; } || echo "no system cmake (expected)"
UV_CACHE_DIR=/tmp/uvcache uv sync 2>&1 | tail -8
.venv/bin/python -c 'import cext, skb, markupsafe, markupsafe._speedups as s; print("cext.add", cext.add(2, 3), "skb.add", skb.add(4, 5), "markupsafe C speedups", s.__file__)'
echo "== git commit and push to a local remote"
git config --global user.email a@example.com; git config --global user.name agent; git config --global init.defaultBranch main
git init -q --bare /tmp/remote.git && git init -q /tmp/repo && cd /tmp/repo
echo hi > a.txt && git add a.txt && git commit -qm "first" && git remote add origin /tmp/remote.git && git push -q -u origin main 2>&1
git --git-dir=/tmp/remote.git log --oneline main
echo "== Playwright drives the bundled browser"
mkdir -p /tmp/site && cat > /tmp/site/index.html <<'H'
<!doctype html><html><head><meta charset="utf-8"><title>treebox i72 check</title><style>body{font-family:sans-serif;background:#f4f1ea;padding:40px} h1{color:#2a6f4e} .box{width:200px;height:80px;background:linear-gradient(90deg,#e4572e,#29335c)}</style></head>
<body><h1>Hello from the slim image</h1><p id="msg">Emoji and CJK: 🌳 树 箱</p><div class="box"></div></body></html>
H
(cd /tmp/site && python -m http.server 8765 >/dev/null 2>&1 &)
sleep 1
mkdir -p /tmp/pw && cd /tmp/pw
playwright-cli open http://127.0.0.1:8765/ | sed -n '1,4p'
playwright-cli eval "document.querySelector('#msg').textContent"
playwright-cli screenshot --filename=/out/local.png | tail -2
playwright-cli goto https://example.com | sed -n '1,4p'
playwright-cli eval "document.title"
playwright-cli screenshot --filename=/out/example.png | tail -2
ps -eo args | grep -m1 'chrome-headless-shell --type' | cut -c1-120
playwright-cli close-all
playwright screenshot https://example.com /out/playwright-lib.png
ls -la /out
echo "ALL CHECKS PASSED"
