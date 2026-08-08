"""LLM 제공자의 응답 처리 테스트.

여기서 볼 것은 **실패를 어떻게 분류하는가** 다. 다시 부르면 되는 실패를 형식
오류로 분류하면 재시도 없이 그 공지를 잃는다.
"""

import unittest
from unittest.mock import MagicMock, patch

from pydantic import ValidationError

from app.llm import GeminiProvider, OpenAIProvider


def provider(response) -> GeminiProvider:
    """SDK 를 가짜로 바꾼 제공자. 실제 API 를 부르지 않는다."""

    with patch("google.genai.Client") as client:
        instance = GeminiProvider(api_key="테스트키", model="테스트모델")
    instance._client = MagicMock()
    instance._client.models.generate_content.return_value = response
    return instance


class GeminiExtractTests(unittest.TestCase):
    def test_empty_response_is_retryable_not_a_schema_error(self) -> None:
        """응답이 비면 다시 부르면 된다. 길이 제한·안전 필터·한도 근처에서 난다."""

        response = MagicMock(text="", candidates=[MagicMock(finish_reason="MAX_TOKENS")])

        with self.assertRaises(Exception) as caught:
            provider(response).extract("본문")

        # ValidationError 로 새면 추출기가 '형식 오류' 로 보고 재시도를 포기한다.
        self.assertNotIsInstance(caught.exception, ValidationError)
        self.assertIn("MAX_TOKENS", str(caught.exception))

    def test_malformed_json_stays_a_schema_error(self) -> None:
        """내용이 있는데 형식이 틀린 것은 다시 불러도 같다. 재시도하지 않는다."""

        with self.assertRaises(ValidationError):
            provider(MagicMock(text="{'entities': ")).extract("본문")

    def test_valid_response_is_parsed(self) -> None:
        payload = '{"entities": [{"type": "공지", "name": "장학금 안내"}], "relations": []}'

        result = provider(MagicMock(text=payload)).extract("본문")

        self.assertEqual(result.entities[0].name, "장학금 안내")


def openai_provider(content, finish_reason: str = "stop") -> OpenAIProvider:
    """SDK 를 가짜로 바꾼 OpenAI 제공자. 실제 API 를 부르지 않는다."""

    with patch("openai.OpenAI"):
        instance = OpenAIProvider(api_key="테스트키", model="테스트모델")
    instance._client = MagicMock()
    resp = MagicMock(choices=[MagicMock(finish_reason=finish_reason)])
    resp.choices[0].message.content = content
    instance._client.chat.completions.create.return_value = resp
    return instance


class OpenAIExtractTests(unittest.TestCase):
    """GeminiProvider 와 같은 실패 분류를 OpenAIProvider 도 지켜야 한다."""

    def test_empty_response_is_retryable_not_a_schema_error(self) -> None:
        with self.assertRaises(Exception) as caught:
            openai_provider("", finish_reason="length").extract("본문")

        self.assertNotIsInstance(caught.exception, ValidationError)
        self.assertIn("length", str(caught.exception))

    def test_malformed_json_stays_a_schema_error(self) -> None:
        with self.assertRaises(ValidationError):
            openai_provider("{'entities': ").extract("본문")

    def test_valid_response_is_parsed(self) -> None:
        payload = '{"entities": [{"type": "공지", "name": "장학금 안내"}], "relations": []}'

        result = openai_provider(payload).extract("본문")

        self.assertEqual(result.entities[0].name, "장학금 안내")


if __name__ == "__main__":
    unittest.main()
