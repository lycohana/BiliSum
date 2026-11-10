"""Task-level prompt directives: 补充要求 (extra instructions) and output language.

The pipeline appends them to the user message of each summary-producing model
step, in labelled sections placed after the prompt's data. With neither set,
messages are returned untouched, so prompts stay byte-identical to before.
The route layer (Agent mode) intercepts these same messages, so an Agent's
pending item carries the same directives without extra plumbing.
"""

from __future__ import annotations

from dataclasses import dataclass

from video_sum_core.models.tasks import TaskOptions, TaskResult

LANGUAGE_NAMES: dict[str, str] = {
    "zh": "简体中文",
    "zh-cn": "简体中文",
    "zh-hans": "简体中文",
    "zh-tw": "繁體中文",
    "zh-hant": "繁體中文",
    "en": "English",
    "ja": "日本語",
    "ko": "한국어",
}


def language_display_name(language: str) -> str:
    return LANGUAGE_NAMES.get(language.strip().lower(), language.strip())


@dataclass(frozen=True, slots=True)
class PromptDirectives:
    extra_instructions: str | None = None
    language: str | None = None

    @classmethod
    def from_options(cls, options: TaskOptions) -> PromptDirectives:
        return cls(extra_instructions=options.extra_instructions, language=options.language)

    @classmethod
    def from_result(cls, result: TaskResult) -> PromptDirectives:
        return cls(extra_instructions=result.extra_instructions, language=result.language)

    @property
    def is_empty(self) -> bool:
        return not self.extra_instructions and not self.language

    def render(self) -> str:
        sections: list[str] = []
        if self.extra_instructions:
            sections.append(
                "【补充要求】（来源：用户为本次任务提供的补充要求。只影响内容取舍和侧重点，"
                "不改变上面规定的输出格式、字段和结构）\n"
                f"{self.extra_instructions}"
            )
        if self.language:
            sections.append(
                "【输出语言】（来源：本次任务指定的输出语言）\n"
                f"请使用 {language_display_name(self.language)} 撰写所有面向读者的文字内容；"
                "JSON 字段名和输出结构保持不变。"
            )
        return "".join(f"\n\n{section}" for section in sections)


def apply_prompt_directives(
    messages: list[dict[str, object]],
    directives: PromptDirectives | None,
) -> list[dict[str, object]]:
    """Append the directive sections to the last text user message.

    Returns ``messages`` itself when there is nothing to add.
    """
    if directives is None or directives.is_empty:
        return messages
    patched = [dict(message) for message in messages]
    for message in reversed(patched):
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            message["content"] = f"{message['content']}{directives.render()}"
            return patched
    patched.append({"role": "user", "content": directives.render().lstrip("\n")})
    return patched
