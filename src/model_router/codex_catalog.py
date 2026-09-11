"""Codex's picker consumes a ModelsResponse catalog, not OpenAI's models list."""
from __future__ import annotations


def build_catalog(exposed: list[dict], models: list[dict]) -> dict:
    capabilities = {model['key']: model for model in models}
    entries = []
    for index, public in enumerate(exposed):
        model = capabilities.get(public['key'], {})
        reasoning = model.get('supports_reasoning', True)
        efforts = ['low', 'medium', 'high', 'xhigh'] if reasoning else ['none']
        entries.append({
            'slug': public['id'],
            'display_name': public['id'],
            'description': 'Model Router · ' + public['id'],
            'visibility': 'list',
            'supported_in_api': True,
            'priority': index,
            'default_reasoning_level': 'high' if reasoning else 'none',
            'supported_reasoning_levels': [
                {'effort': effort, 'description': effort} for effort in efforts
            ],
            'shell_type': 'shell_command',
            'base_instructions': 'You are Codex, a coding agent. Help the user with their coding tasks.',
            'support_verbosity': False,
            'supports_reasoning_summaries': reasoning,
            'default_reasoning_summary': 'none',
            'supports_parallel_tool_calls': False,
            'truncation_policy': {'mode': 'bytes', 'limit': 10000},
            'context_window': model.get('context_window', 131072),
            'input_modalities': ['text', 'image'] if model.get('supports_vision') is True else ['text'],
            'experimental_supported_tools': [],
        })
    return {'models': entries}
