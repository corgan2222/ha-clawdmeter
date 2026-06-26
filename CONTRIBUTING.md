# Contributing

Thanks for helping improve Clawdmeter!

## Branch policy

`main` is protected and **cannot be pushed to directly** — every change goes through a pull
request:

1. Create a branch and commit your change there.
2. Open a pull request against `main`.
3. CI must pass: **Ruff** (lint + format), **Pytest** (tests), **Hassfest** and **HACS**
   (validation).
4. Merge once all checks are green.

Force-pushing to and deleting `main` are disabled for everyone.

## Development

Tests use
[pytest-homeassistant-custom-component](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component):

```bash
pip install -r requirements_test.txt
pytest tests/
ruff check .
ruff format --check .
```

After a notable change, bump `version` in `custom_components/clawdmeter/manifest.json`.
