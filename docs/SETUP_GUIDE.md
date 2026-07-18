# Setup Guide

## Python

- Requirements: Python 3.12 or higher, uv

```bash
# Check versions.
python3 --version
uv --version

# Setup a project.
uv init
uv add --dev ruff pytest ty taskipy
```

## JavaScript / TypeScript

- Requirements: Node.js 20 or higher, npm 10 or higher

```bash
# Check versions.
node --version
npm --version

# Install tools.
npm install --global pnpm vite-plus @voidzero-dev/vite-plus-core@latest
pnpm --version

# Setup a project.
vp install
```
