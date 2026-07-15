"""Tests for cost tracking logic (pure functions, no DB needed)."""

from __future__ import annotations

from agentbox.cost.tracker import (
    estimate_compute_cost,
    estimate_llm_cost,
    get_model_rates,
)
from agentbox.settings import settings


def test_get_model_rates_known_model():
    """Should return known rates for deepseek-chat."""
    rates = get_model_rates("deepseek-chat")
    assert rates["input"] == 0.00027
    assert rates["output"] == 0.00110


def test_get_model_rates_fallback():
    """Should fall back to settings defaults for unknown models."""
    rates = get_model_rates("unknown-model")
    assert rates["input"] == settings.cost_per_1k_input_tokens
    assert rates["output"] == settings.cost_per_1k_output_tokens


def test_estimate_llm_cost_deepseek():
    """1000 input + 500 output tokens with deepseek-chat."""
    cost = estimate_llm_cost("deepseek-chat", input_tokens=1000, output_tokens=500)
    expected_input = (1000 * 0.00027) / 1000  # 0.00027
    expected_output = (500 * 0.00110) / 1000  # 0.00055
    expected = round(expected_input + expected_output, 8)
    assert cost == expected


def test_estimate_llm_cost_zero_tokens():
    """Zero tokens should cost zero."""
    cost = estimate_llm_cost("deepseek-chat", 0, 0)
    assert cost == 0.0


def test_estimate_compute_cost():
    """1 hour of compute at $0.06/hr."""
    cost = estimate_compute_cost(3600)
    expected = round(3600 * 0.0000167, 8)
    assert cost == expected


def test_estimate_compute_cost_zero():
    """Zero duration should cost zero."""
    cost = estimate_compute_cost(0)
    assert cost == 0.0
