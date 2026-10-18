import hashlib
import json
import os
from collections import defaultdict
from typing import Annotated, List

import PIL
from pydantic import BaseModel

from marker.logger import get_logger
from marker.schema.blocks import Block
from marker.services import BaseService

logger = get_logger()


class AntigravityService(BaseService):
    """File-backed LLM service that bridges Marker's LLM processors to an Antigravity agent session."""

    agy_cache_dir: Annotated[
        str,
        "Directory to write pending LLM block requests and read agent JSON responses.",
    ] = ""

    def __init__(self, config: BaseModel | dict | None = None):
        super().__init__(config)
        if not self.agy_cache_dir:
            self.agy_cache_dir = os.environ.get(
                "MARKER_AGY_CACHE_DIR", "/tmp/marker_agy_cache"
            )
        self._call_counts: dict[str, int] = defaultdict(int)

    def _request_id(
        self, prompt: str, block: Block | None, response_schema: type[BaseModel]
    ) -> str:
        schema_name = response_schema.__name__
        if block is not None and getattr(block, "id", None) is not None:
            safe_block = str(block.id).strip("/").replace("/", "_")
            base_key = f"{safe_block}__{schema_name}"
            idx = self._call_counts[base_key]
            self._call_counts[base_key] += 1
            return f"{base_key}__{idx}"
        digest = hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:10]
        return f"doc__{schema_name}__{digest}"

    def __call__(
        self,
        prompt: str,
        image: PIL.Image.Image | List[PIL.Image.Image] | None,
        block: Block | None,
        response_schema: type[BaseModel],
        max_retries: int | None = None,
        timeout: int | None = None,
    ) -> dict:
        req_id = self._request_id(prompt, block, response_schema)
        req_dir = os.path.join(self.agy_cache_dir, "requests")
        resp_dir = os.path.join(self.agy_cache_dir, "responses")
        img_dir = os.path.join(self.agy_cache_dir, "images")

        resp_path = os.path.join(resp_dir, f"{req_id}.json")
        if os.path.exists(resp_path):
            try:
                with open(resp_path, "r", encoding="utf-8") as f:
                    data = response_schema.model_validate(json.load(f)).model_dump()
                if block:
                    block.update_metadata(llm_tokens_used=0, llm_request_count=1)
                return data
            except Exception as e:
                logger.warning(f"Invalid agent response {resp_path}: {e}")
                return {}

        os.makedirs(req_dir, exist_ok=True)
        os.makedirs(resp_dir, exist_ok=True)
        os.makedirs(img_dir, exist_ok=True)

        if image is None:
            images = []
        elif isinstance(image, list):
            images = image
        else:
            images = [image]

        image_paths = []
        for idx, img in enumerate(images):
            img_path = os.path.abspath(os.path.join(img_dir, f"{req_id}_{idx}.webp"))
            img.save(img_path, format="WEBP")
            image_paths.append(img_path)

        req_payload = {
            "id": req_id,
            "block_id": str(block.id) if block is not None else None,
            "schema_name": response_schema.__name__,
            "json_schema": response_schema.model_json_schema(),
            "images": image_paths,
            "response_path": os.path.abspath(resp_path),
            "prompt": prompt,
        }
        req_path = os.path.join(req_dir, f"{req_id}.json")
        with open(req_path, "w", encoding="utf-8") as f:
            json.dump(req_payload, f, indent=2, ensure_ascii=False)

        # Return empty dict on Pass 1 so Marker skips rewriting until the agent populates responses/
        return {}
