"""Screenshot Reader: a vision model fills in the Archive's item form from a screenshot."""

import os

from mmgu.core.modules import Module, SettingField


def _router():
    from mmgu.addons.screenshot_reader.routes import router

    return router


def _setup(hall):
    from mmgu.addons.screenshot_reader import reader

    hall.ext.add("archive.readers", reader.read_item, module="screenshot_reader")
    hall.ext.add("vault.readers", reader.read_bank, module="screenshot_reader")


MODULE = Module(
    id="screenshot_reader",
    name="Screenshot Reader",
    plain_name="AI item reading",
    description=(
        "Reads item and bank screenshots with a vision model (your own Ollama, or Claude) and fills in the "
        "Archive form for a person to check. Settings and a test page: /addons/screenshot-reader."
    ),
    kind="addon",
    requires=["archive"],
    default_enabled=False,
    admin_url="/addons/screenshot-reader",
    tos_risk="caution",
    tos_notice=(
        "This reads screenshots of the game that members upload and pulls item details out of them. "
        "The Monsters & Memories user agreement forbids software that reads or mines information the game "
        "produces without permission, and a screenshot reader may count. It never touches the game itself, "
        "but whether to use it is your guild's call and your responsibility."
    ),
    settings=[
        SettingField(
            "provider",
            "Which model reads screenshots",
            "select",
            "ollama",
            "ollama: a model on your own computer (free, private). anthropic: Claude (paid, needs an API key).",
            options=["ollama", "anthropic"],
        ),
        SettingField(
            "ollama_url",
            "Ollama address",
            "text",
            os.environ.get("MMGU_OLLAMA_URL") or "http://localhost:11434",
            "Where Ollama is running, as seen from the hall's server.",
        ),
        SettingField(
            "ollama_model",
            "Ollama model",
            "text",
            "qwen3-vl:8b",
            "A vision model you've pulled with `ollama pull`. qwen3-vl:8b fits an 11 GB GPU.",
        ),
        SettingField(
            "ollama_think",
            "Let the Ollama model think before answering",
            "bool",
            False,
            "Only matters for thinking models such as qwen3-vl. Much slower (minutes on a home GPU), rarely better.",
        ),
        SettingField("anthropic_model", "Claude model", "text", "claude-haiku-4-5", "Used when the reader is Claude."),
        SettingField(
            "anthropic_api_key",
            "Anthropic API key",
            "secret",
            None,
            "Only needed for Claude. Leave blank to use the ANTHROPIC_API_KEY environment variable.",
        ),
    ],
    router=_router,
    setup=_setup,
)
