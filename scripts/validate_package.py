"""Offline source/metadata/translation checks; complements official CI validators."""
import ast
import json
from pathlib import Path
import tomllib
import struct

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components/openclaw_voice"


def load(name):
    return json.loads((COMPONENT / name).read_text())


def main():
    manifest = load("manifest.json")
    assert manifest["domain"] == "openclaw_voice"
    assert manifest["config_flow"] is True
    assert manifest["integration_type"] == "service"
    assert manifest["iot_class"] == "local_polling"
    assert manifest["requirements"] == []
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert project["project"]["version"] == manifest["version"].replace("-dev.", ".dev")
    strings = load("strings.json")
    assert strings == load("translations/en.json")
    for category in ["config", "options"]:
        for step in strings[category]["step"].values():
            assert set(step.get("data", {})) <= set(step.get("data_description", {}))
    parsed = {}
    for file in COMPONENT.glob("*.py"):
        parsed[file.name] = ast.parse(file.read_text(), filename=str(file))
    for name, tree in parsed.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_attr_translation_key" for t in node.targets):
                key = node.value.value
                platform = name.removesuffix(".py")
                assert key in strings["entity"][platform]
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"validation_error", "speech_error"} and node.args and isinstance(node.args[0], ast.Constant):
                assert node.args[0].value in strings["exceptions"]
    assert "async_step_reconfigure" in (COMPONENT / "config_flow.py").read_text()
    for name, size in [("icon.png", 256), ("icon@2x.png", 512)]:
        image = (COMPONENT / "brand" / name).read_bytes()
        assert image.startswith(b"\x89PNG\r\n\x1a\n")
        assert struct.unpack(">II", image[16:24]) == (size, size)
    print(json.dumps({"result": "PASS", "version": manifest["version"], "modules": len(parsed),
                      "checks": ["metadata", "syntax", "flow-descriptions", "entity-and-error-translations", "reconfigure", "no-vendor-dependencies"]}))


if __name__ == "__main__":
    main()
