"""Optional: build a standalone GoodWe.app menubar bundle with py2app.

    ./.venv/bin/python -m pip install py2app
    ./.venv/bin/python setup.py py2app

The result is dist/GoodWe.app, which you can drag to /Applications. LSUIElement
keeps it menubar-only (no Dock icon). Running ``python -m goodwe_menubar``
directly works fine too and is easier for development.
"""

from setuptools import setup

APP = ["goodwe_menubar/__main__.py"]
OPTIONS = {
    "argv_emulation": False,
    "packages": ["rumps", "goodwe"],
    "plist": {
        "CFBundleName": "GoodWe",
        "CFBundleDisplayName": "GoodWe Solar",
        "CFBundleIdentifier": "com.local.goodwe-menubar",
        "CFBundleVersion": "0.1.0",
        "LSUIElement": True,  # menubar-only, no Dock icon
        "LSMinimumSystemVersion": "12.0",
    },
}

setup(
    app=APP,
    name="GoodWe",
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
