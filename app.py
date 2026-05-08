import json
import logging
import math
import os
import uuid
import subprocess
import tempfile
import base64
import threading
import re
import time
import shutil
import hmac
import hashlib
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs
from io import BytesIO

import requests
from PIL import Image
from elevenlabs.client import ElevenLabs
from elevenlabs.core.api_error import ApiError
from openai import OpenAI
from flask import (
    Flask, render_template, request, redirect, url_for,
    jsonify, flash, abort, Response,
)
from flask_socketio import join_room, leave_room

from config import Config
from extensions import db, migrate, socketio
from models import (
    Child, CartoonAvatar, Character,
    Cartoon, CartoonScene, CartoonParticipant, CartoonCharacterLink,
)


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)
    migrate.init_app(app, db)
    socketio.init_app(app, async_mode="threading", cors_allowed_origins="*")

    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs(app.config["GENERATED_FOLDER"], exist_ok=True)

    # ---- file logger so we can see callbacks even without a terminal
    log_path = os.path.join(os.path.dirname(__file__), "cartoons.log")
    file_handler = logging.FileHandler(log_path)
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    app.logger.addHandler(file_handler)
    app.logger.setLevel(logging.INFO)

    @app.context_processor
    def inject_voice_options():
        return {"voice_options": app.config.get("ELEVENLABS_PRESET_VOICES", [])}

    # cartoon_id -> runtime pipeline state for UI locking/progress
    pipeline_runtime_state = {}
    pipeline_runtime_lock = threading.Lock()
    template_jobs_lock = threading.Lock()
    pixverse_worker_lock = threading.Lock()
    pixverse_worker_started = False
    template2_worker_lock = threading.Lock()
    template2_worker_started = False
    template3_worker_lock = threading.Lock()
    template3_worker_started = False
    template4_worker_lock = threading.Lock()
    template4_worker_started = False
    kling_rate_lock = threading.Lock()
    kling_next_request_ts = 0.0
    avatar_worker_lock = threading.Lock()
    avatar_worker_started = False

    # ------------------------------------------------------------------ helpers

    def allowed_file(filename):
        return (
            "." in filename
            and filename.rsplit(".", 1)[1].lower()
            in app.config["ALLOWED_EXTENSIONS"]
        )

    def templates_root_dir() -> str:
        return os.path.join(app.static_folder, "templates")

    def templates2_root_dir() -> str:
        return os.path.join(app.static_folder, "templates2")

    def templates3_root_dir() -> str:
        return os.path.join(app.static_folder, "templates3")

    def templates4_root_dir() -> str:
        return os.path.join(app.static_folder, "templates4")

    def safe_template_dir(template_name: str) -> str | None:
        root = os.path.abspath(templates_root_dir())
        candidate = os.path.abspath(os.path.join(root, template_name))
        try:
            if os.path.commonpath([root, candidate]) != root:
                return None
        except ValueError:
            return None
        if not os.path.isdir(candidate):
            return None
        return candidate

    def safe_template2_dir(template_name: str) -> str | None:
        root = os.path.abspath(templates2_root_dir())
        candidate = os.path.abspath(os.path.join(root, template_name))
        try:
            if os.path.commonpath([root, candidate]) != root:
                return None
        except ValueError:
            return None
        if not os.path.isdir(candidate):
            return None
        return candidate

    def safe_template3_dir(template_name: str) -> str | None:
        root = os.path.abspath(templates3_root_dir())
        candidate = os.path.abspath(os.path.join(root, template_name))
        try:
            if os.path.commonpath([root, candidate]) != root:
                return None
        except ValueError:
            return None
        if not os.path.isdir(candidate):
            return None
        return candidate

    def safe_template4_dir(template_name: str) -> str | None:
        root = os.path.abspath(templates4_root_dir())
        candidate = os.path.abspath(os.path.join(root, template_name))
        try:
            if os.path.commonpath([root, candidate]) != root:
                return None
        except ValueError:
            return None
        if not os.path.isdir(candidate):
            return None
        return candidate

    def template_fragment_sort_key(filename: str):
        stem = os.path.splitext(filename)[0]
        match = re.search(r"\d+", stem)
        if match:
            return 0, int(match.group()), stem.lower(), filename.lower()
        return 1, stem.lower(), filename.lower()

    def template_prompts_path(template_dir: str) -> str:
        return os.path.join(template_dir, ".scene_prompts.json")

    def load_template_prompts(template_dir: str) -> dict[str, str]:
        path = template_prompts_path(template_dir)
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}
            result = {}
            for key, value in data.items():
                if isinstance(key, str) and isinstance(value, str):
                    result[key] = value
            return result
        except Exception:
            return {}

    def save_template_prompts(template_dir: str, prompts: dict[str, str]) -> None:
        path = template_prompts_path(template_dir)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(prompts, f, ensure_ascii=False, indent=2)

    def template3_options_path(template_dir: str) -> str:
        return os.path.join(template_dir, ".scene_options3.json")

    def load_template3_options(template_dir: str) -> dict[str, dict]:
        path = template3_options_path(template_dir)
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}
            result: dict[str, dict] = {}
            for key, value in data.items():
                if isinstance(key, str) and isinstance(value, dict):
                    result[key] = value
            return result
        except Exception:
            return {}

    def save_template3_options(template_dir: str, options_by_file: dict[str, dict]) -> None:
        path = template3_options_path(template_dir)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(options_by_file, f, ensure_ascii=False, indent=2)

    def template_jobs_path(template_dir: str) -> str:
        return os.path.join(template_dir, ".scene_jobs.json")

    def template4_scenes_path(template_dir: str) -> str:
        return os.path.join(template_dir, ".scenes4.json")

    def load_template4_scenes(template_dir: str) -> list[dict]:
        path = template4_scenes_path(template_dir)
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                return []
            result = []
            for item in data:
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    result.append(item)
            return result
        except Exception:
            return []

    def save_template4_scenes(template_dir: str, scenes: list[dict]) -> None:
        path = template4_scenes_path(template_dir)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(scenes, f, ensure_ascii=False, indent=2)

    def get_scene_and_step(template_dir: str, scene_id: str, step_id: str | None = None):
        scenes = load_template4_scenes(template_dir)
        scene = next((s for s in scenes if str(s.get("id")) == str(scene_id)), None)
        if not scene:
            return scenes, None, None
        step = None
        if step_id is not None:
            steps = scene.get("steps") if isinstance(scene.get("steps"), list) else []
            step = next((x for x in steps if str(x.get("id")) == str(step_id)), None)
        return scenes, scene, step

    def find_template4_task_id_in_log(template_name: str, scene_id: str) -> str:
        log_path = os.path.join(os.path.dirname(__file__), "cartoons.log")
        if not os.path.isfile(log_path):
            return ""
        pattern = re.compile(
            r"Template4 KLING task created template=(?P<template>.+?) scene=(?P<scene>\S+) task_id=(?P<task>\d+)"
        )
        try:
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()[-5000:]
            for line in reversed(lines):
                m = pattern.search(line)
                if not m:
                    continue
                if m.group("template") == template_name and m.group("scene") == scene_id:
                    return (m.group("task") or "").strip()
        except Exception:
            return ""
        return ""

    def load_template_jobs(template_dir: str) -> dict[str, dict]:
        path = template_jobs_path(template_dir)
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}
            result = {}
            for key, value in data.items():
                if isinstance(key, str) and isinstance(value, dict):
                    result[key] = value
            return result
        except Exception:
            return {}

    def is_mask_detection_busy(template_dir: str, file_name: str) -> bool:
        jobs = load_template_jobs(template_dir)
        job = jobs.get(file_name) if isinstance(jobs, dict) else None
        if not isinstance(job, dict):
            return False
        return (
            str(job.get("operation", "")).lower() == "detect_masks"
            and str(job.get("status", "")).lower() in {"queued", "processing"}
        )

    def save_template_jobs(template_dir: str, jobs: dict[str, dict]) -> None:
        path = template_jobs_path(template_dir)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(jobs, f, ensure_ascii=False, indent=2)

    def template_pixverse_cache_path(template_dir: str) -> str:
        return os.path.join(template_dir, ".pixverse_cache.json")

    def load_template_pixverse_cache(template_dir: str) -> dict[str, dict]:
        path = template_pixverse_cache_path(template_dir)
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}
            result = {}
            for key, value in data.items():
                if isinstance(key, str) and isinstance(value, dict):
                    result[key] = value
            return result
        except Exception:
            return {}

    def save_template_pixverse_cache(template_dir: str, cache: dict[str, dict]) -> None:
        path = template_pixverse_cache_path(template_dir)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)

    def get_valid_pixverse_cache_entry(
        template_dir: str,
        file_name: str,
        input_path: str,
    ) -> dict | None:
        cache = load_template_pixverse_cache(template_dir)
        entry = cache.get(file_name)
        if not isinstance(entry, dict):
            return None
        try:
            stat = os.stat(input_path)
            same_file = (
                int(entry.get("size", -1)) == int(stat.st_size)
                and int(entry.get("mtime_ns", -1)) == int(stat.st_mtime_ns)
            )
            if not same_file:
                return None
            return entry
        except Exception:
            return None

    def update_pixverse_cache_entry(
        template_dir: str,
        file_name: str,
        input_path: str,
        **fields,
    ) -> None:
        try:
            stat = os.stat(input_path)
        except Exception:
            return
        cache = load_template_pixverse_cache(template_dir)
        current = cache.get(file_name, {}) if isinstance(cache.get(file_name), dict) else {}
        current.update(fields)
        current["size"] = int(stat.st_size)
        current["mtime_ns"] = int(stat.st_mtime_ns)
        current["updated_at"] = datetime.now(timezone.utc).isoformat()
        cache[file_name] = current
        save_template_pixverse_cache(template_dir, cache)

    def mask_bbox_from_url(mask_url: str) -> dict | None:
        try:
            resp = requests.get(mask_url, timeout=30)
            resp.raise_for_status()
            image = Image.open(BytesIO(resp.content)).convert("RGBA")
            alpha = image.getchannel("A")
            bbox = alpha.getbbox()
            if not bbox:
                # Fallback for non-alpha masks
                gray = image.convert("L")
                bw = gray.point(lambda p: 255 if p > 10 else 0)
                bbox = bw.getbbox()
            if not bbox:
                return None
            left, top, right, bottom = bbox
            width, height = image.size
            if width <= 0 or height <= 0:
                return None
            return {
                "x": left / width,
                "y": top / height,
                "w": max(0, right - left) / width,
                "h": max(0, bottom - top) / height,
            }
        except Exception:
            return None

    def parse_ffprobe_fps(rate: str | None) -> float | None:
        value = str(rate or "").strip()
        if not value:
            return None
        if "/" in value:
            left, right = value.split("/", 1)
            try:
                num = float(left)
                den = float(right)
                if den == 0:
                    return None
                return num / den
            except Exception:
                return None
        try:
            return float(value)
        except Exception:
            return None

    def probe_video_timing(input_path: str) -> tuple[float, float]:
        duration = 0.0
        fps = 24.0
        try:
            proc = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=r_frame_rate,avg_frame_rate:format=duration",
                    "-of",
                    "json",
                    input_path,
                ],
                capture_output=True,
                text=True,
                timeout=12,
                check=True,
            )
            data = json.loads(proc.stdout or "{}")
            streams = data.get("streams") or []
            stream = streams[0] if streams else {}
            fps_val = parse_ffprobe_fps(stream.get("avg_frame_rate")) or parse_ffprobe_fps(stream.get("r_frame_rate"))
            if fps_val and fps_val > 0:
                fps = fps_val
            duration_val = float((data.get("format") or {}).get("duration") or 0)
            if duration_val > 0:
                duration = duration_val
        except Exception:
            pass
        return duration, fps

    def has_audio_stream(input_path: str) -> bool:
        try:
            proc = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "a:0",
                    "-show_entries",
                    "stream=index",
                    "-of",
                    "json",
                    input_path,
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            data = json.loads(proc.stdout or "{}")
            streams = data.get("streams") or []
            return len(streams) > 0
        except Exception:
            return False

    def remux_original_audio_into_video(original_video_path: str, generated_video_path: str) -> bool:
        if not ffmpeg_available():
            return False
        if not has_audio_stream(original_video_path):
            return False
        tmp_output = f"{generated_video_path}.with_original_audio.mp4"
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    generated_video_path,
                    "-i",
                    original_video_path,
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-c:v",
                    "copy",
                    "-c:a",
                    "aac",
                    "-shortest",
                    tmp_output,
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            os.replace(tmp_output, generated_video_path)
            return True
        except Exception as exc:
            app.logger.warning(
                "Failed to remux original audio template video=%s generated=%s: %s",
                original_video_path,
                generated_video_path,
                exc,
            )
            try:
                if os.path.exists(tmp_output):
                    os.remove(tmp_output)
            except Exception:
                pass
            return False

    def detect_masks_for_video_second(
        template_dir: str,
        file_name: str,
        input_path: str,
        second: int,
        *,
        aggressive: bool = False,
        force: bool = False,
    ) -> dict:
        sec = max(0, int(second or 0))
        entry = get_valid_pixverse_cache_entry(template_dir, file_name, input_path) or {}
        timeline = entry.get("detected_masks_timeline")
        if not isinstance(timeline, dict):
            timeline = {}
        cached_second = timeline.get(str(sec))
        if cached_second and not force:
            if not aggressive:
                return cached_second
            cached_masks = cached_second.get("masks") if isinstance(cached_second, dict) else None
            if isinstance(cached_masks, list):
                has_character_like = any(
                    is_character_like_mask_name(str(m.get("mask_name", "")))
                    for m in cached_masks
                    if isinstance(m, dict)
                )
                if has_character_like:
                    return cached_second

        video_media_id = entry.get("video_media_id")
        if video_media_id is None:
            video_media_id = pixverse_upload_video(input_path)
            update_pixverse_cache_entry(
                template_dir,
                file_name,
                input_path,
                video_media_id=int(video_media_id),
            )
            entry = get_valid_pixverse_cache_entry(template_dir, file_name, input_path) or {}
            timeline = entry.get("detected_masks_timeline")
            if not isinstance(timeline, dict):
                timeline = {}

        duration = float(entry.get("video_duration_sec") or 0)
        fps = float(entry.get("video_fps") or 0)
        if duration <= 0 or fps <= 0:
            duration, fps = probe_video_timing(input_path)

        def normalize_masks(raw_masks: list[dict]) -> list[dict]:
            result = []
            for item in raw_masks or []:
                mask_id = item.get("mask_id")
                if mask_id is None:
                    continue
                mask_name = str(item.get("mask_name", ""))
                mask_url = item.get("mask_url")
                result.append(
                    {
                        "mask_id": str(mask_id),
                        "mask_name": mask_name,
                        "mask_url": mask_url,
                        "bbox": mask_bbox_from_url(mask_url) if mask_url else None,
                    }
                )
            return result

        # Detailed detection: probe nearby moments around this second to catch transient character masks.
        offsets = [0.0, -0.35, 0.35, -0.7, 0.7]
        if aggressive:
            offsets = [0.0, -0.2, 0.2, -0.4, 0.4, -0.7, 0.7, -1.0, 1.0, -1.4, 1.4]
        max_second = max(0.0, duration - (1.0 / max(fps, 1.0))) if duration > 0 else float(sec)
        candidates = []
        for off in offsets:
            probe_second = max(0.0, min(max_second, float(sec) + off))
            keyframe_id = max(1, int(round(probe_second * fps)) + 1)
            try:
                mask_resp = pixverse_post_json(
                    "/openapi/v2/video/mask/selection",
                    {
                        "video_media_id": int(video_media_id),
                        "keyframe_id": keyframe_id,
                    },
                )
            except RuntimeError as exc:
                err = str(exc)
                if "Object detection failed" in err or "Invalid keyframe" in err:
                    continue
                raise
            returned_keyframe = int(mask_resp.get("keyframe_id", keyframe_id) or keyframe_id)
            raw_masks = mask_resp.get("mask_info") or []
            score = 0
            for item in raw_masks:
                score += character_mask_priority(str(item.get("mask_name", "")))
            candidates.append(
                {
                    "probe_second": round(probe_second, 3),
                    "keyframe_id": returned_keyframe,
                    "raw_masks": raw_masks,
                    "score": score,
                }
            )

        if not candidates:
            raise RuntimeError("PixVerse не вернул маски для этой секунды видео.")

        candidates.sort(key=lambda x: x["score"], reverse=True)
        best = candidates[0]
        returned_keyframe = int(best["keyframe_id"])
        detected_masks = normalize_masks(best["raw_masks"])
        attempts_debug = []
        for cand in candidates:
            attempts_debug.append(
                {
                    "probe_second": cand["probe_second"],
                    "keyframe_id": int(cand["keyframe_id"]),
                    "score": int(cand["score"]),
                    "mask_names": [str(i.get("mask_name", "")) for i in (cand["raw_masks"] or [])],
                }
            )

        frame_entry = {
            "second": sec,
            "keyframe_id": returned_keyframe,
            "probe_second": best["probe_second"],
            "masks": detected_masks,
            "mask_attempts": attempts_debug,
            "aggressive": bool(aggressive),
        }
        timeline[str(sec)] = frame_entry
        update_fields = {
            "video_media_id": int(video_media_id),
            "detected_masks_timeline": timeline,
            "video_fps": float(fps),
            "video_duration_sec": float(duration),
        }
        if sec == 0 or not entry.get("detected_masks"):
            update_fields["detected_masks"] = detected_masks
            update_fields["keyframe_id"] = returned_keyframe
        update_pixverse_cache_entry(
            template_dir,
            file_name,
            input_path,
            **update_fields,
        )
        return frame_entry

    def detect_masks_timeline_for_video(
        template_dir: str,
        file_name: str,
        input_path: str,
        *,
        aggressive: bool = False,
    ) -> tuple[int, int, int, int]:
        duration, _fps = probe_video_timing(input_path)
        total_seconds = max(1, int(math.ceil(duration))) if duration > 0 else 1
        detected = 0
        skipped = 0
        failed = 0
        for second in range(total_seconds):
            try:
                existing = get_valid_pixverse_cache_entry(template_dir, file_name, input_path) or {}
                timeline = existing.get("detected_masks_timeline")
                if isinstance(timeline, dict) and timeline.get(str(second)):
                    skipped += 1
                    continue
                detect_masks_for_video_second(
                    template_dir,
                    file_name,
                    input_path,
                    second,
                    aggressive=aggressive,
                    force=False,
                )
                detected += 1
            except Exception as exc:
                failed += 1
                app.logger.error(
                    "Mask detect failed template=%s file=%s second=%s: %s",
                    os.path.basename(template_dir),
                    file_name,
                    second,
                    exc,
                )
        return detected, skipped, failed, total_seconds

    def detect_masks_for_video(
        template_dir: str,
        file_name: str,
        input_path: str,
        *,
        force: bool = False,
    ) -> dict:
        entry = None if force else get_valid_pixverse_cache_entry(template_dir, file_name, input_path)
        if entry and entry.get("detected_masks"):
            return entry

        if entry and entry.get("video_media_id"):
            video_media_id = int(entry["video_media_id"])
        else:
            video_media_id = pixverse_upload_video(input_path)
            update_pixverse_cache_entry(
                template_dir,
                file_name,
                input_path,
                video_media_id=video_media_id,
            )

        mask_attempts = [None, 1, 5, 10, 15, 30, 45, 60, 90, 120]
        best_resp = None
        best_score = -10**9
        best_frame = 1
        for frame_candidate in mask_attempts:
            payload = {"video_media_id": video_media_id}
            if frame_candidate is not None:
                payload["keyframe_id"] = frame_candidate
            try:
                mask_resp = pixverse_post_json("/openapi/v2/video/mask/selection", payload)
            except Exception:
                continue
            keyframe_id = int(mask_resp.get("keyframe_id", frame_candidate or 1) or 1)
            mask_info = mask_resp.get("mask_info") or []
            score = 0
            for item in mask_info:
                n = str(item.get("mask_name", "")).lower()
                score += character_mask_priority(n)
            if score > best_score:
                best_score = score
                best_resp = mask_resp
                best_frame = keyframe_id

        if not best_resp:
            raise RuntimeError("PixVerse не вернул маски для этого видео.")

        raw_masks = best_resp.get("mask_info") or []
        detected_masks = []
        for item in raw_masks:
            mask_id = item.get("mask_id")
            if mask_id is None:
                continue
            mask_name = str(item.get("mask_name", ""))
            mask_url = item.get("mask_url")
            detected_masks.append(
                {
                    "mask_id": str(mask_id),
                    "mask_name": mask_name,
                    "mask_url": mask_url,
                    "bbox": mask_bbox_from_url(mask_url) if mask_url else None,
                }
            )

        update_pixverse_cache_entry(
            template_dir,
            file_name,
            input_path,
            video_media_id=video_media_id,
            keyframe_id=best_frame,
            detected_masks=detected_masks,
        )
        return get_valid_pixverse_cache_entry(template_dir, file_name, input_path) or {}

    def get_pixverse_swap_cache(
        template_dir: str,
        file_name: str,
        input_path: str,
    ) -> dict | None:
        entry = get_valid_pixverse_cache_entry(template_dir, file_name, input_path)
        if not entry:
            return None
        if (
            entry.get("video_media_id") is None
            or entry.get("keyframe_id") is None
            or entry.get("mask_id") is None
        ):
            return None
        if not is_character_like_mask_name(str(entry.get("mask_name", ""))):
            return None
        return entry

    def set_pixverse_swap_cache(
        template_dir: str,
        file_name: str,
        input_path: str,
        *,
        video_media_id: int,
        keyframe_id: int,
        mask_id: str,
        mask_name: str = "",
    ) -> None:
        update_pixverse_cache_entry(
            template_dir,
            file_name,
            input_path,
            video_media_id=int(video_media_id),
            keyframe_id=int(keyframe_id),
            mask_id=str(mask_id),
            mask_name=mask_name,
        )

    def update_template_job(
        template_dir: str,
        file_name: str,
        *,
        status: str,
        message: str | None = None,
        prompt: str | None = None,
        output_url: str | None = None,
        pixverse_video_id: int | None = None,
        operation: str | None = None,
        event_name: str = "template_job_update",
        room_prefix: str = "template",
    ) -> None:
        with template_jobs_lock:
            jobs = load_template_jobs(template_dir)
            job = jobs.get(file_name, {})
            job["status"] = status
            if message is not None:
                job["message"] = message
            if prompt is not None:
                job["prompt"] = prompt
            if output_url is not None:
                job["output_url"] = output_url
            if pixverse_video_id is not None:
                job["pixverse_video_id"] = pixverse_video_id
            if operation is not None:
                job["operation"] = operation
            job["updated_at"] = datetime.now(timezone.utc).isoformat()
            jobs[file_name] = job
            save_template_jobs(template_dir, jobs)
            template_name = os.path.basename(template_dir.rstrip("/"))
            socketio.emit(
                event_name,
                (
                    {
                        "template_name": template_name,
                        "file_name": file_name,
                        "job": job,
                        "scene": next(
                            (s for s in load_template4_scenes(template_dir) if str(s.get("id")) == str(file_name)),
                            None,
                        ),
                    }
                    if event_name == "template4_job_update"
                    else {
                        "template_name": template_name,
                        "file_name": file_name,
                        "job": job,
                    }
                ),
                room=f"{room_prefix}_{template_name}",
            )

    def server_url():
        return app.config["SERVER"] or request.host_url.rstrip("/")

    def redis_client():
        try:
            import redis
        except Exception as exc:
            raise RuntimeError("Python package 'redis' is not installed") from exc
        return redis.Redis.from_url(app.config["REDIS_URL"], decode_responses=True)

    def pixverse_headers(content_type_json: bool = False) -> dict:
        headers = {
            "API-KEY": app.config["PIXVERSE_AI_API_KEY"],
            "Ai-Trace-Id": str(uuid.uuid4()),
        }
        if content_type_json:
            headers["Content-Type"] = "application/json"
        return headers

    def pixverse_post_json(path: str, payload: dict) -> dict:
        if not app.config.get("PIXVERSE_AI_API_KEY"):
            raise RuntimeError("PIXVERSE_AI_API_KEY не задан в .env.")
        url = f"{app.config['PIXVERSE_BASE_URL']}{path}"
        resp = requests.post(
            url,
            headers=pixverse_headers(content_type_json=True),
            json=payload,
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("ErrCode", 0) != 0:
            raise RuntimeError(f"PixVerse API error: {data.get('ErrMsg') or data}")
        return data.get("Resp") or {}

    def pixverse_get_json(path: str) -> dict:
        if not app.config.get("PIXVERSE_AI_API_KEY"):
            raise RuntimeError("PIXVERSE_AI_API_KEY не задан в .env.")
        url = f"{app.config['PIXVERSE_BASE_URL']}{path}"
        resp = requests.get(
            url,
            headers=pixverse_headers(content_type_json=False),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("ErrCode", 0) != 0:
            raise RuntimeError(f"PixVerse API error: {data.get('ErrMsg') or data}")
        return data.get("Resp") or {}

    def pixverse_upload_video(local_path: str) -> int:
        url = f"{app.config['PIXVERSE_BASE_URL']}/openapi/v2/media/upload"
        with open(local_path, "rb") as f:
            files = {"file": (os.path.basename(local_path), f)}
            resp = requests.post(
                url,
                headers=pixverse_headers(content_type_json=False),
                files=files,
                timeout=120,
            )
        resp.raise_for_status()
        data = resp.json()
        if data.get("ErrCode", 0) != 0:
            raise RuntimeError(f"PixVerse upload video error: {data.get('ErrMsg') or data}")
        media_id = (data.get("Resp") or {}).get("media_id")
        if media_id is None:
            raise RuntimeError("PixVerse upload video error: media_id not found")
        return int(media_id)

    def pixverse_upload_image_from_url(image_url: str) -> int:
        tmp_path = None
        try:
            parsed = urlparse(image_url)
            suffix = os.path.splitext(parsed.path)[1].lower()
            if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
                suffix = ".jpg"
            dl = requests.get(image_url, timeout=60)
            dl.raise_for_status()
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(dl.content)
                tmp_path = tmp.name

            url = f"{app.config['PIXVERSE_BASE_URL']}/openapi/v2/image/upload"
            with open(tmp_path, "rb") as f:
                files = {"image": (os.path.basename(tmp_path), f)}
                resp = requests.post(
                    url,
                    headers=pixverse_headers(content_type_json=False),
                    files=files,
                    timeout=120,
                )
            resp.raise_for_status()
            data = resp.json()
            if data.get("ErrCode", 0) != 0:
                raise RuntimeError(f"PixVerse upload image error: {data.get('ErrMsg') or data}")
            img_id = (data.get("Resp") or {}).get("img_id")
            if img_id is None:
                raise RuntimeError("PixVerse upload image error: img_id not found")
            return int(img_id)
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)

    def pixverse_upload_image_from_file(local_path: str) -> int:
        if not os.path.isfile(local_path):
            raise RuntimeError(f"Файл изображения не найден: {local_path}")
        url = f"{app.config['PIXVERSE_BASE_URL']}/openapi/v2/image/upload"
        with open(local_path, "rb") as f:
            files = {"image": (os.path.basename(local_path), f)}
            resp = requests.post(
                url,
                headers=pixverse_headers(content_type_json=False),
                files=files,
                timeout=120,
            )
        resp.raise_for_status()
        data = resp.json()
        if data.get("ErrCode", 0) != 0:
            raise RuntimeError(f"PixVerse upload image error: {data.get('ErrMsg') or data}")
        img_id = (data.get("Resp") or {}).get("img_id")
        if img_id is None:
            raise RuntimeError("PixVerse upload image error: img_id not found")
        return int(img_id)

    def select_best_mask(mask_info: list[dict]) -> str | None:
        if not mask_info:
            return None
        keywords = ("person", "human", "child", "girl", "boy", "woman", "man")
        for item in mask_info:
            name = str(item.get("mask_name", "")).lower()
            if any(k in name for k in keywords):
                mask_id = item.get("mask_id")
                if mask_id is not None:
                    return str(mask_id)
        first = mask_info[0]
        if first.get("mask_id") is not None:
            return str(first.get("mask_id"))
        return None

    def is_person_mask_name(mask_name: str) -> bool:
        name = str(mask_name or "").lower().strip()
        keywords = ("person", "human", "child", "girl", "boy", "woman", "man")
        return any(k in name for k in keywords)

    def is_character_like_mask_name(mask_name: str) -> bool:
        name = str(mask_name or "").lower().strip()
        if is_person_mask_name(name):
            return True
        # PixVerse often labels animated characters via clothing/body parts
        char_keywords = (
            "dress", "shirt", "jacket", "coat", "skirt", "pants",
            "face", "head", "hair", "body", "character",
            "sheep", "bird", "cat", "dog", "bear", "rabbit", "bunny", "fox", "wolf",
        )
        return any(k in name for k in char_keywords)

    def character_mask_priority(mask_name: str) -> int:
        name = str(mask_name or "").lower().strip()
        if not name:
            return -100
        if is_person_mask_name(name):
            return 100
        if any(k in name for k in ("face", "head")):
            return 90
        if "hair" in name:
            return 80
        if "body" in name or "character" in name:
            return 70
        if any(k in name for k in ("dress", "shirt", "jacket", "coat", "skirt", "pants")):
            return 45
        if any(k in name for k in ("sheep", "bird", "cat", "dog", "rabbit", "bunny", "fox", "wolf")):
            return 75
        if "background" in name or "bg" in name:
            return -100
        if any(k in name for k in ("chair", "table", "teddy", "bear", "toy", "wall", "floor", "window", "door")):
            return -100
        return 0

    def select_person_mask(mask_info: list[dict]) -> str | None:
        if not mask_info:
            return None
        for item in mask_info:
            if is_person_mask_name(str(item.get("mask_name", ""))):
                mask_id = item.get("mask_id")
                if mask_id is not None:
                    return str(mask_id)
        return None

    def select_character_like_mask(mask_info: list[dict]) -> str | None:
        if not mask_info:
            return None
        # Prefer explicit person labels first
        person_mask = select_person_mask(mask_info)
        if person_mask:
            return person_mask
        # Then accept animated-character proxies with priority:
        # face/head/hair/body > clothes, while skipping obvious props/background.
        best_mask_id = None
        best_score = -10**9
        for item in mask_info:
            mask_id = item.get("mask_id")
            if mask_id is None:
                continue
            name = str(item.get("mask_name", "")).lower().strip()
            if not is_character_like_mask_name(name):
                continue
            score = character_mask_priority(name)
            if score > best_score:
                best_score = score
                best_mask_id = str(mask_id)
        return best_mask_id

    def is_expired_signed_url(url: str) -> bool:
        try:
            parsed = urlparse(url)
            qs = parse_qs(parsed.query)
            signed_at = (qs.get("X-Amz-Date") or qs.get("x-amz-date") or [None])[0]
            expires_raw = (qs.get("X-Amz-Expires") or qs.get("x-amz-expires") or [None])[0]
            if not signed_at or not expires_raw:
                return False
            signed_dt = datetime.strptime(signed_at, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            expires_sec = int(expires_raw)
            expire_at = signed_dt.timestamp() + expires_sec
            return time.time() > expire_at
        except Exception:
            return False

    def refresh_avatar_url_if_possible(avatar: CartoonAvatar) -> str | None:
        current = (avatar.image_url or "").strip() or None
        if not avatar.task_id:
            return current
        try:
            data = fetch_avatar_task_result(avatar.task_id)
            if data and apply_task_result(avatar, data):
                db.session.commit()
            refreshed = (avatar.image_url or "").strip() or None
            return refreshed or current
        except Exception:
            return current

    def build_avatar_image_candidate(image_ref: str) -> dict | None:
        ref = (image_ref or "").strip()
        if not ref:
            return None
        if ref.startswith("/static/"):
            local_path = os.path.join(app.root_path, ref.lstrip("/"))
            if os.path.isfile(local_path):
                return {"kind": "file", "value": local_path}
            return None
        if ref.startswith("http://") or ref.startswith("https://"):
            return {"kind": "url", "value": ref}
        if os.path.isfile(ref):
            return {"kind": "file", "value": ref}
        return None

    def resolve_child_avatar_candidates(child_name: str) -> tuple[list[dict], Child, int]:
        child = Child.query.filter(Child.name.ilike(child_name)).order_by(Child.id.desc()).first()
        if not child:
            raise RuntimeError(f"Ребёнок «{child_name}» не найден.")
        selected_avatar = child.selected_avatar
        if not selected_avatar or selected_avatar.status != "completed":
            raise RuntimeError(
                f"У ребёнка «{child_name}» не выбран активный готовый аватар. "
                "Выберите аватар на странице ребёнка."
            )

        image_ref = (selected_avatar.image_url or "").strip()
        if not image_ref or (image_ref.startswith("http") and is_expired_signed_url(image_ref)):
            image_ref = refresh_avatar_url_if_possible(selected_avatar) or ""

        candidate = build_avatar_image_candidate(image_ref)
        if not candidate:
            raise RuntimeError(
                f"Активный аватар ребёнка «{child_name}» недоступен. "
                "Перегенерируйте аватар и выберите его заново."
            )
        return [candidate], child, selected_avatar.id

    def resolve_prompt_child_tokens(prompt: str) -> tuple[str, list[int]]:
        img_ids: list[int] = []
        idx_by_name: dict[str, int] = {}
        urls_by_name: dict[str, str] = {}

        pattern = re.compile(r"\{child:\s*([^}]+)\}", flags=re.IGNORECASE)

        def _replace(match: re.Match) -> str:
            child_name = match.group(1).strip()
            if not child_name:
                raise RuntimeError("Пустое имя в токене {child: ...}.")
            key = child_name.lower()
            if key in idx_by_name:
                return f"@img{idx_by_name[key]}"
            avatar_url = urls_by_name.get(key)
            if avatar_url is None:
                avatar_candidates, _child, selected_avatar_id = resolve_child_avatar_candidates(child_name)
                upload_error = None
                img_id = None
                for candidate in avatar_candidates:
                    try:
                        if candidate.get("kind") == "file":
                            candidate_path = candidate.get("value", "")
                            img_id = pixverse_upload_image_from_file(candidate_path)
                            avatar_url = f"file:{candidate_path}"
                        else:
                            candidate_url = candidate.get("value", "")
                            img_id = pixverse_upload_image_from_url(candidate_url)
                            avatar_url = candidate_url
                        app.logger.info(
                            "Using selected avatar for child=%s avatar_id=%s source=%s",
                            child_name,
                            selected_avatar_id,
                            avatar_url,
                        )
                        break
                    except requests.HTTPError as exc:
                        upload_error = exc
                        continue
                    except Exception as exc:
                        upload_error = exc
                        continue
                if img_id is None:
                    raise RuntimeError(
                        f"Не удалось использовать аватар ребёнка «{child_name}» для PixVerse: {upload_error}"
                    )
                urls_by_name[key] = avatar_url or ""
            else:
                img_id = pixverse_upload_image_from_url(avatar_url)
            idx = len(img_ids)
            img_ids.append(img_id)
            idx_by_name[key] = idx
            return f"@img{idx}"

        resolved_prompt = pattern.sub(_replace, prompt)
        return resolved_prompt, img_ids

    def extract_explicit_mask_id(prompt: str) -> tuple[str, str | None]:
        pattern = re.compile(r"\{mask:\s*([0-9A-Za-z_-]+)\}", flags=re.IGNORECASE)
        match = pattern.search(prompt or "")
        if not match:
            return prompt, None
        mask_id = match.group(1).strip()
        cleaned = pattern.sub("", prompt).strip()
        return cleaned, (mask_id or None)

    def resolve_explicit_mask_reference(
        template_dir: str,
        file_name: str,
        input_path: str,
        mask_ref: str,
        *,
        video_media_id: int,
    ) -> tuple[str, int, str]:
        ref = str(mask_ref or "").strip()
        ref_norm = ref.lower()
        if not ref_norm:
            raise RuntimeError("Пустой токен маски в {mask:...}.")

        def find_in_mask_info(mask_info: list[dict], keyframe_id_value: int) -> tuple[str, int, str] | None:
            best = None
            for item in mask_info or []:
                raw_id = item.get("mask_id")
                if raw_id is None:
                    continue
                mid = str(raw_id).strip()
                mname = str(item.get("mask_name", "")).strip()
                if mid.lower() == ref_norm or mname.lower() == ref_norm:
                    score = character_mask_priority(mname)
                    candidate = (score, mid, int(keyframe_id_value), mname)
                    if best is None or candidate[0] > best[0]:
                        best = candidate
            if best is None:
                return None
            return best[1], best[2], best[3]

        # 1) Fast path: use cached per-second timeline.
        entry = get_valid_pixverse_cache_entry(template_dir, file_name, input_path) or {}
        timeline = entry.get("detected_masks_timeline")
        if isinstance(timeline, dict):
            matches: list[tuple[int, int, str, int, str]] = []
            for second_key, frame in timeline.items():
                if not isinstance(frame, dict):
                    continue
                try:
                    second = int(second_key)
                except Exception:
                    second = 0
                keyframe_id = int(frame.get("keyframe_id") or 1)
                found = find_in_mask_info(frame.get("masks") or [], keyframe_id)
                if found:
                    mid, kf, mname = found
                    matches.append((character_mask_priority(mname), second, mid, kf, mname))
            if matches:
                # Prefer strongest character-like mask; for ties choose earliest second.
                matches.sort(key=lambda x: (-x[0], x[1]))
                _score, _second, mid, kf, mname = matches[0]
                return mid, kf, mname or ref

        # 2) Fallback: probe a small keyframe set and find exact id/name match.
        duration_sec, fps = probe_video_timing(input_path)
        max_keyframe = None
        if duration_sec > 0 and fps > 0:
            max_keyframe = max(1, int(math.floor(duration_sec * fps)))
        raw_mask_attempts = [None, 1, 5, 10, 15, 30, 45, 60, 90, 120]
        mask_attempts = []
        for candidate in raw_mask_attempts:
            if candidate is None:
                mask_attempts.append(candidate)
                continue
            if max_keyframe is not None and candidate > max_keyframe:
                continue
            if candidate not in mask_attempts:
                mask_attempts.append(candidate)

        for frame_candidate in mask_attempts:
            try:
                payload = {"video_media_id": int(video_media_id)}
                if frame_candidate is not None:
                    payload["keyframe_id"] = frame_candidate
                mask_resp = pixverse_post_json("/openapi/v2/video/mask/selection", payload)
                keyframe_id = int(mask_resp.get("keyframe_id", frame_candidate or 1) or 1)
                found = find_in_mask_info(mask_resp.get("mask_info") or [], keyframe_id)
                if found:
                    mid, kf, mname = found
                    app.logger.info(
                        "Explicit mask resolved template=%s file=%s mask_ref=%s keyframe_id=%s mask_id=%s mask_name=%s",
                        os.path.basename(template_dir),
                        file_name,
                        ref,
                        kf,
                        mid,
                        mname,
                    )
                    return mid, kf, mname or ref
            except RuntimeError as exc:
                err = str(exc)
                if "Object detection failed" in err or "Invalid keyframe" in err:
                    continue
                raise

        raise RuntimeError(
            f"Маска «{ref}» не найдена в доступных кадрах этого видео. "
            "Сначала нажмите «Определить маски», затем используйте точный mask_id/имя из списка."
        )

    def run_pixverse_modify_job(
        template_name: str,
        file_name: str,
        prompt: str,
        *,
        mode: str = "modify",
    ) -> str:
        template_dir = safe_template_dir(template_name)
        if not template_dir:
            raise RuntimeError("Папка шаблона не найдена.")

        input_path = os.path.join(template_dir, file_name)
        if not os.path.isfile(input_path):
            raise RuntimeError("Файл фрагмента не найден.")

        prompt_wo_mask, explicit_mask_id = extract_explicit_mask_id(prompt)
        resolved_prompt, img_ids = resolve_prompt_child_tokens(prompt_wo_mask)
        requested_mode = (mode or "modify").strip().lower()
        if requested_mode not in {"modify", "swap"}:
            requested_mode = "modify"
        use_swap_for_child = requested_mode == "swap"
        mask_id = None
        keyframe_id = 1
        selected_name = ""
        if use_swap_for_child and not img_ids:
            raise RuntimeError(
                "Режим Swap требует ссылку на аватар в промпте (например, {child: Юра})."
            )
        if img_ids and use_swap_for_child:
            cache_entry = get_valid_pixverse_cache_entry(template_dir, file_name, input_path)
            if explicit_mask_id:
                if cache_entry and cache_entry.get("video_media_id"):
                    video_media_id = int(cache_entry["video_media_id"])
                else:
                    update_template_job(
                        template_dir,
                        file_name,
                        status="processing",
                        message="Загружаю видео в PixVerse…",
                        prompt=prompt,
                    )
                    video_media_id = pixverse_upload_video(input_path)
                    update_pixverse_cache_entry(
                        template_dir,
                        file_name,
                        input_path,
                        video_media_id=video_media_id,
                        keyframe_id=1,
                    )
                mask_id, keyframe_id, selected_name = resolve_explicit_mask_reference(
                    template_dir,
                    file_name,
                    input_path,
                    explicit_mask_id,
                    video_media_id=video_media_id,
                )
                set_pixverse_swap_cache(
                    template_dir,
                    file_name,
                    input_path,
                    video_media_id=video_media_id,
                    keyframe_id=keyframe_id,
                    mask_id=mask_id,
                    mask_name=selected_name,
                )
                update_template_job(
                    template_dir,
                    file_name,
                    status="processing",
                    message=(
                        f"Использую явно указанную маску {explicit_mask_id} "
                        f"(resolved: mask={mask_id}, keyframe={keyframe_id})."
                    ),
                    prompt=prompt,
                )
            else:
                cached = get_pixverse_swap_cache(template_dir, file_name, input_path)
                if cached:
                    video_media_id = int(cached["video_media_id"])
                    keyframe_id = int(cached["keyframe_id"])
                    mask_id = str(cached["mask_id"])
                    selected_name = str(cached.get("mask_name", ""))
                    update_template_job(
                        template_dir,
                        file_name,
                        status="processing",
                        message=(
                            "Использую сохранённую маску для этого видео-фрагмента "
                            f"(mask={mask_id}, keyframe={keyframe_id})."
                        ),
                        prompt=prompt,
                    )
                    app.logger.info(
                        "PixVerse mask cache hit template=%s file=%s video_media_id=%s keyframe_id=%s mask_id=%s mask_name=%s",
                        template_name,
                        file_name,
                        video_media_id,
                        keyframe_id,
                        mask_id,
                        selected_name,
                    )
                else:
                    update_template_job(
                        template_dir,
                        file_name,
                        status="processing",
                        message="Загружаю видео в PixVerse…",
                        prompt=prompt,
                    )
                    video_media_id = pixverse_upload_video(input_path)
                    update_template_job(
                        template_dir,
                        file_name,
                        status="processing",
                        message="Определяю маску объекта для замены…",
                    )
                    mask_error = None
                    # PixVerse keyframe_id is 1-based; omitting it lets PixVerse choose its default.
                    duration_sec, fps = probe_video_timing(input_path)
                    max_keyframe = None
                    if duration_sec > 0 and fps > 0:
                        max_keyframe = max(1, int(math.floor(duration_sec * fps)))
                    raw_mask_attempts = [None, 1, 5, 10, 15, 30, 45, 60, 90, 120]
                    mask_attempts = []
                    for candidate in raw_mask_attempts:
                        if candidate is None:
                            mask_attempts.append(candidate)
                            continue
                        if max_keyframe is not None and candidate > max_keyframe:
                            continue
                        if candidate not in mask_attempts:
                            mask_attempts.append(candidate)
                    if len(mask_attempts) == 1 and mask_attempts[0] is None and max_keyframe:
                        mask_attempts.append(min(1, max_keyframe))
                    for frame_candidate in mask_attempts:
                        try:
                            mask_payload = {"video_media_id": video_media_id}
                            if frame_candidate is not None:
                                mask_payload["keyframe_id"] = frame_candidate
                            mask_resp = pixverse_post_json(
                                "/openapi/v2/video/mask/selection",
                                mask_payload,
                            )
                            keyframe_id = int(mask_resp.get("keyframe_id", frame_candidate or 1) or 1)
                            mask_info = mask_resp.get("mask_info") or []
                            app.logger.info(
                                "PixVerse masks detected template=%s file=%s requested_keyframe=%s returned_keyframe=%s masks=%s",
                                template_name,
                                file_name,
                                frame_candidate,
                                keyframe_id,
                                [
                                    {
                                        "mask_id": item.get("mask_id"),
                                        "mask_name": item.get("mask_name"),
                                    }
                                    for item in mask_info
                                ],
                            )
                            mask_id = select_character_like_mask(mask_info)
                            if mask_id:
                                for item in mask_info:
                                    if str(item.get("mask_id")) == str(mask_id):
                                        selected_name = str(item.get("mask_name", ""))
                                        break
                                app.logger.info(
                                    "PixVerse mask selected template=%s file=%s keyframe_id=%s mask_id=%s mask_name=%s",
                                    template_name,
                                    file_name,
                                    keyframe_id,
                                    mask_id,
                                    selected_name,
                                )
                                set_pixverse_swap_cache(
                                    template_dir,
                                    file_name,
                                    input_path,
                                    video_media_id=video_media_id,
                                    keyframe_id=keyframe_id,
                                    mask_id=mask_id,
                                    mask_name=selected_name,
                                )
                                break
                        except RuntimeError as exc:
                            mask_error = str(exc)
                            if (
                                "Object detection failed" in mask_error
                                or "Invalid keyframe" in mask_error
                            ):
                                continue
                            else:
                                raise
                    if not mask_id:
                        detail = f" ({mask_error})" if mask_error else ""
                        raise RuntimeError(
                            "PixVerse не смог определить объект для замены на этом видео-фрагменте. "
                            "Среди масок PixVerse не было подходящей маски персонажа "
                            "(person/girl/boy/child или animated-character маски типа dress/face/hair). "
                            "Проверьте лог PixVerse masks detected, чтобы увидеть, что сервис распознал в кадре."
                            f"{detail}"
                        )
        else:
            update_template_job(
                template_dir,
                file_name,
                status="processing",
                message="Загружаю видео в PixVerse…",
                prompt=prompt,
            )
            video_media_id = pixverse_upload_video(input_path)
            # In MODIFY mode, if explicit mask token was provided, resolve it to
            # a concrete mask/keyframe and use as a targeting hint in prompt text.
            if img_ids and explicit_mask_id:
                mask_id, keyframe_id, selected_name = resolve_explicit_mask_reference(
                    template_dir,
                    file_name,
                    input_path,
                    explicit_mask_id,
                    video_media_id=video_media_id,
                )
                update_template_job(
                    template_dir,
                    file_name,
                    status="processing",
                    message=(
                        f"В режиме Modify использую таргет-маску {explicit_mask_id} "
                        f"(resolved: mask={mask_id}, keyframe={keyframe_id}, label={selected_name})."
                    ),
                    prompt=prompt,
                )

        update_template_job(
            template_dir,
            file_name,
            status="processing",
            message=(
                "Запускаю задачу Swap в PixVerse…"
                if (img_ids and use_swap_for_child)
                else "Запускаю задачу Modify в PixVerse…"
            ),
        )

        if img_ids and use_swap_for_child:
            # For character replacement, PixVerse docs require SWAP endpoint.
            if len(img_ids) > 1:
                raise RuntimeError(
                    "Для одного видео-фрагмента поддерживается замена только одним активным аватаром."
                )
            endpoint_path = "/openapi/v2/video/swap/generate"
            payload = {
                "video_media_id": video_media_id,
                "keyframe_id": keyframe_id,
                "mask_id": str(mask_id),
                "img_id": int(img_ids[0]),
                "quality": app.config["PIXVERSE_QUALITY"],
            }
            payload_fallback = {
                "video_media_id": video_media_id,
                "keyframe_id": keyframe_id,
                "mask_id": str(mask_id),
                "img_id": int(img_ids[0]),
                "quality": "360p",
            }
        else:
            endpoint_path = "/openapi/v2/video/modify/generate"
            modify_prompt = resolved_prompt
            modify_prompt_simple = resolved_prompt
            modify_prompt_avatar_only = None
            if img_ids:
                user_requests_text_edit = bool(
                    re.search(
                        r"(надпис|текст|subtitle|caption|label|word|слово|букв|символ)",
                        resolved_prompt or "",
                        flags=re.IGNORECASE,
                    )
                )
                identity_refs = ", ".join(f"@img{i}" for i in range(len(img_ids)))
                target_hint = ""
                if mask_id:
                    target_hint = (
                        f" Target mask hint: mask_id={mask_id}, keyframe_id={keyframe_id}"
                        + (f", label={selected_name}." if selected_name else ".")
                    )
                text_guard = (
                    ""
                    if user_requests_text_edit
                    else "Do not add text, subtitles, symbols, logos or watermarks. "
                )
                modify_prompt = (
                    f"Use {identity_refs} as identity reference image(s). "
                    "Replace the ENTIRE target character in the video, not only clothes. "
                    "Must replace face, head, hair, skin, body and outfit consistently in all frames. "
                    "Preserve original motion, expression timing, lip-sync timing, camera, background and composition. "
                    f"{text_guard}"
                    f"{target_hint}\n"
                    f"{resolved_prompt}"
                )
                modify_prompt_simple = (
                    f"Use {identity_refs} to replace the target character identity. "
                    "Replace whole character (face/head/body/outfit), keep scene and motion unchanged. "
                    f"{'' if user_requests_text_edit else 'No text or symbols. '}"
                    f"{resolved_prompt}"
                )
                if user_requests_text_edit:
                    avatar_only_user_prompt = re.sub(
                        r"[^.!?\n]*?(надпис|текст|subtitle|caption|label|word|слово|букв|символ)[^.!?\n]*[.!?\n]?",
                        " ",
                        resolved_prompt,
                        flags=re.IGNORECASE,
                    )
                    avatar_only_user_prompt = re.sub(r"\s+", " ", avatar_only_user_prompt).strip()
                    modify_prompt_avatar_only = (
                        f"Use {identity_refs} to replace only the target character identity. "
                        "Replace whole character (face/head/body/outfit), keep scene/camera/motion unchanged. "
                        "Do not modify text objects, signs, subtitles or inscriptions in the scene. "
                        "No extra symbols.\n"
                        f"{avatar_only_user_prompt or 'Replace target character with @img0.'}"
                    )
            payload = {
                "video_media_id": video_media_id,
                "prompt": modify_prompt,
                "quality": app.config["PIXVERSE_QUALITY"],
            }
            # For MODIFY flow with child references, pass uploaded image ids explicitly.
            if img_ids:
                payload["img_ids"] = [int(x) for x in img_ids]
                if len(img_ids) == 1:
                    payload["img_id"] = int(img_ids[0])
            payload_fallback = dict(payload)
            payload_fallback["quality"] = "360p"

        video_url = None
        last_error = None
        attempts = [("Основная попытка", payload)]
        if payload_fallback:
            attempts.append(("Fallback качество 360p", payload_fallback))
        if endpoint_path == "/openapi/v2/video/modify/generate" and img_ids:
            img0 = int(img_ids[0])
            attempts.append(
                (
                    "Fallback только img_id + упрощённый промпт",
                    {
                        "video_media_id": video_media_id,
                        "prompt": modify_prompt_simple,
                        "quality": "360p",
                        "img_id": img0,
                    },
                )
            )
            attempts.append(
                (
                    "Fallback только img_ids + упрощённый промпт",
                    {
                        "video_media_id": video_media_id,
                        "prompt": modify_prompt_simple,
                        "quality": "360p",
                        "img_ids": [int(x) for x in img_ids],
                    },
                )
            )
            if modify_prompt_avatar_only:
                attempts.append(
                    (
                        "Fallback avatar-only (без изменения текста в сцене)",
                        {
                            "video_media_id": video_media_id,
                            "prompt": modify_prompt_avatar_only,
                            "quality": "360p",
                            "img_id": img0,
                        },
                    )
                )

        for attempt_idx, (attempt_label, current_payload) in enumerate(attempts, start=1):
            if attempt_idx > 1:
                update_template_job(
                    template_dir,
                    file_name,
                    status="processing",
                    message=f"{attempt_label}: повторный запуск PixVerse…",
                )

            modify_resp = pixverse_post_json(endpoint_path, current_payload)
            video_id = int(modify_resp.get("video_id", 0) or 0)
            if not video_id:
                raise RuntimeError("PixVerse generation endpoint не вернул video_id.")

            update_template_job(
                template_dir,
                file_name,
                status="processing",
                message=f"{attempt_label}: ожидаю готовность видео в PixVerse…",
                pixverse_video_id=video_id,
            )

            max_attempts = app.config["PIXVERSE_POLL_MAX_ATTEMPTS"]
            interval = app.config["PIXVERSE_POLL_INTERVAL_SECONDS"]
            current_error = None
            for _ in range(max_attempts):
                status_resp = pixverse_get_json(f"/openapi/v2/video/result/{video_id}")
                status_code = int(status_resp.get("status", 0) or 0)
                if status_code == 1:
                    video_url = status_resp.get("url")
                    if not video_url:
                        raise RuntimeError("PixVerse вернул success без URL.")
                    break
                if status_code in (7, 8):
                    detail = (
                        status_resp.get("message")
                        or status_resp.get("msg")
                        or status_resp.get("error")
                        or status_resp.get("fail_reason")
                        or status_resp.get("detail")
                        or ""
                    )
                    app.logger.warning(
                        "PixVerse result failed template=%s file=%s attempt=%s status=%s response=%s",
                        template_name,
                        file_name,
                        attempt_label,
                        status_code,
                        status_resp,
                    )
                    if not detail:
                        try:
                            detail = json.dumps(status_resp, ensure_ascii=False)
                        except Exception:
                            detail = str(status_resp)
                    suffix = f": {detail}" if detail else ""
                    current_error = RuntimeError(
                        f"PixVerse завершил задачу с ошибкой (status={status_code}){suffix}."
                    )
                    break
                time.sleep(interval)

            if video_url:
                break
            if current_error:
                last_error = current_error
                continue
            last_error = RuntimeError("Таймаут ожидания результата PixVerse.")

        if not video_url:
            raise last_error or RuntimeError("Не удалось получить результат PixVerse.")

        update_template_job(
            template_dir,
            file_name,
            status="processing",
            message="Скачиваю готовое видео…",
        )
        processed_dir = os.path.join(template_dir, "processed")
        os.makedirs(processed_dir, exist_ok=True)
        output_name = f"{os.path.splitext(file_name)[0]}__pixverse_{int(time.time())}.mp4"
        if 'video_id' in locals() and video_id:
            output_name = f"{os.path.splitext(file_name)[0]}__pixverse_{video_id}.mp4"
        output_path = os.path.join(processed_dir, output_name)
        out = requests.get(video_url, timeout=120)
        out.raise_for_status()
        with open(output_path, "wb") as f:
            f.write(out.content)

        # Keep original speech timing: preserve source audio track on top of PixVerse video.
        remux_original_audio_into_video(input_path, output_path)

        return f"/static/templates/{template_name}/processed/{output_name}"

    def enqueue_pixverse_job(
        template_name: str,
        file_name: str,
        prompt: str,
        *,
        mode: str = "modify",
        options: dict | None = None,
    ) -> None:
        # Redis мог подняться после старта Flask, поэтому пытаемся (пере)запустить воркер перед enqueue.
        ensure_pixverse_worker_started()
        requested_mode = (mode or "modify").strip().lower()
        if requested_mode not in {"modify", "swap", "detect_masks"}:
            requested_mode = "modify"
        job = {
            "job_id": uuid.uuid4().hex,
            "template_name": template_name,
            "file_name": file_name,
            "prompt": prompt,
            "mode": requested_mode,
            "queued_at": datetime.now(timezone.utc).isoformat(),
        }
        if isinstance(options, dict):
            job["options"] = options
        client = redis_client()
        queue_name = app.config["PIXVERSE_QUEUE_NAME"]
        queue_size = client.lpush(queue_name, json.dumps(job, ensure_ascii=False))
        app.logger.info(
            "Enqueued PixVerse job id=%s template=%s file=%s mode=%s queue=%s size=%s",
            job["job_id"],
            template_name,
            file_name,
            requested_mode,
            queue_name,
            queue_size,
        )
        # Safety net: if long-running worker wasn't alive, kick one-shot consumer.
        try:
            socketio.start_background_task(pixverse_queue_worker_once)
        except Exception:
            pass

    def enqueue_avatar_job(avatar_id: int) -> str:
        ensure_avatar_worker_started()
        job_id = uuid.uuid4().hex
        payload = {
            "job_id": job_id,
            "avatar_id": avatar_id,
            "queued_at": datetime.now(timezone.utc).isoformat(),
        }
        client = redis_client()
        queue_name = app.config["AVATAR_QUEUE_NAME"]
        queue_size = client.lpush(queue_name, json.dumps(payload, ensure_ascii=False))
        app.logger.info(
            "Enqueued avatar job id=%s avatar=%s queue=%s size=%s",
            job_id,
            avatar_id,
            queue_name,
            queue_size,
        )
        return job_id

    def process_pixverse_job_payload(payload: dict) -> None:
        template_name = payload.get("template_name", "")
        file_name = payload.get("file_name", "")
        prompt = payload.get("prompt", "")
        mode = (payload.get("mode") or "modify").strip().lower()
        if mode not in {"modify", "swap", "detect_masks"}:
            mode = "modify"
        options = payload.get("options") if isinstance(payload.get("options"), dict) else {}
        job_id = payload.get("job_id", "")
        with app.app_context():
            template_dir = safe_template_dir(template_name)
            if not template_dir:
                app.logger.warning("PixVerse job skipped: template not found job=%s template=%s", job_id, template_name)
                return
            app.logger.info(
                "PixVerse worker picked job id=%s template=%s file=%s mode=%s",
                job_id,
                template_name,
                file_name,
                mode,
            )
            try:
                if mode == "detect_masks":
                    input_path = os.path.join(template_dir, file_name)
                    if not os.path.isfile(input_path):
                        raise RuntimeError("Файл шаблона не найден.")
                    aggressive = bool(options.get("aggressive"))
                    update_template_job(
                        template_dir,
                        file_name,
                        status="processing",
                        message=(
                            "Определяю маски по таймлайну видео (агрессивный режим)…"
                            if aggressive
                            else "Определяю маски по таймлайну видео…"
                        ),
                        operation="detect_masks",
                    )
                    detected, skipped, failed, _total = detect_masks_timeline_for_video(
                        template_dir,
                        file_name,
                        input_path,
                        aggressive=aggressive,
                    )
                    update_template_job(
                        template_dir,
                        file_name,
                        status="completed",
                        message=(
                            f"Маски определены: новых {detected}, уже были {skipped}, ошибок {failed}."
                        ),
                        operation="detect_masks",
                    )
                else:
                    output_url = run_pixverse_modify_job(
                        template_name,
                        file_name,
                        prompt,
                        mode=mode,
                    )
                    update_template_job(
                        template_dir,
                        file_name,
                        status="completed",
                        message=f"Готово ({mode})",
                        output_url=output_url,
                        operation=mode,
                    )
            except Exception as exc:
                app.logger.exception(
                    "PixVerse job failed template=%s file=%s: %s",
                    template_name,
                    file_name,
                    exc,
                )
                update_template_job(
                    template_dir,
                    file_name,
                    status="failed",
                    message=str(exc),
                    operation=mode,
                )

    def pixverse_queue_worker_once():
        try:
            client = redis_client()
            item = client.brpop(app.config["PIXVERSE_QUEUE_NAME"], timeout=1)
            if not item:
                return
            _, raw_payload = item
            payload = json.loads(raw_payload)
            process_pixverse_job_payload(payload)
        except Exception as exc:
            app.logger.error("PixVerse one-shot worker error: %s", exc)

    def pixverse_queue_worker():
        app.logger.info("PixVerse worker started")
        while True:
            try:
                client = redis_client()
                item = client.brpop(app.config["PIXVERSE_QUEUE_NAME"], timeout=5)
                if not item:
                    continue
                _, raw_payload = item
                payload = json.loads(raw_payload)
                process_pixverse_job_payload(payload)
            except Exception as exc:
                app.logger.error("PixVerse queue worker error: %s", exc)
                time.sleep(5)

    def eleven_creative_headers(content_type_json: bool = True) -> dict:
        api_key = (app.config.get("ELEVENLABS_API_KEY") or "").strip()
        if not api_key:
            raise RuntimeError("ELEVENLABS_API_KEY не задан в .env.")
        headers = {
            "xi-api-key": api_key,
            "Accept": "application/json",
        }
        if content_type_json:
            headers["Content-Type"] = "application/json"
        return headers

    def eleven_creative_url(path: str) -> str:
        base = (app.config.get("ELEVENLABS_CREATIVE_BASE_URL") or app.config.get("ELEVENLABS_BASE_URL") or "").rstrip("/")
        if not base:
            raise RuntimeError("ELEVENLABS_CREATIVE_BASE_URL не задан.")
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if not path.startswith("/"):
            path = f"/{path}"
        return f"{base}{path}"

    def elevenlabs_sdk_client() -> ElevenLabs:
        api_key = (app.config.get("ELEVENLABS_API_KEY") or "").strip()
        if not api_key:
            raise RuntimeError("ELEVENLABS_API_KEY не задан в .env.")
        base_url = (app.config.get("ELEVENLABS_CREATIVE_BASE_URL") or app.config.get("ELEVENLABS_BASE_URL") or "").strip()
        if not base_url:
            raise RuntimeError("ELEVENLABS_CREATIVE_BASE_URL не задан.")
        return ElevenLabs(api_key=api_key, base_url=base_url.rstrip("/"))

    def elevenlabs_sdk_preflight() -> ElevenLabs:
        client = elevenlabs_sdk_client()
        try:
            client.user.get()
        except ApiError as exc:
            raise RuntimeError(f"Ошибка доступа к ElevenLabs API: status={exc.status_code}, body={exc.body}") from exc

        # If Studio API is blocked, Image/Video programmatic flow is unavailable for this account.
        try:
            client.studio.projects.list()
        except ApiError as exc:
            body = exc.body if isinstance(exc.body, dict) else {"detail": str(exc.body)}
            detail = body.get("detail") if isinstance(body, dict) else None
            status = detail.get("status") if isinstance(detail, dict) else None
            if exc.status_code == 403 and status == "invalid_subscription":
                raise RuntimeError(
                    "Studio API недоступен для этого аккаунта. "
                    "Нужен whitelist от ElevenLabs (даже на платном тарифе)."
                ) from exc
            raise RuntimeError(f"Ошибка Studio API через SDK: status={exc.status_code}, body={exc.body}") from exc
        return client

    def eleven_creative_extract_generation_id(payload: dict) -> str | None:
        candidates = [
            payload.get("generation_id"),
            payload.get("id"),
            payload.get("job_id"),
            (payload.get("data") or {}).get("generation_id") if isinstance(payload.get("data"), dict) else None,
            (payload.get("data") or {}).get("id") if isinstance(payload.get("data"), dict) else None,
            (payload.get("result") or {}).get("generation_id") if isinstance(payload.get("result"), dict) else None,
            (payload.get("result") or {}).get("id") if isinstance(payload.get("result"), dict) else None,
        ]
        for item in candidates:
            if item is not None and str(item).strip():
                return str(item).strip()
        return None

    def eleven_creative_extract_output_url(payload: dict) -> str | None:
        def get_nested_url(data: dict, key_chain: list[str]) -> str | None:
            current = data
            for key in key_chain:
                if not isinstance(current, dict):
                    return None
                current = current.get(key)
            if isinstance(current, str) and current.startswith(("http://", "https://")):
                return current
            return None

        checks = [
            ["output_url"],
            ["url"],
            ["video_url"],
            ["result_url"],
            ["data", "output_url"],
            ["data", "url"],
            ["data", "video_url"],
            ["result", "output_url"],
            ["result", "url"],
            ["result", "video_url"],
            ["output", "url"],
            ["output", "video_url"],
        ]
        for chain in checks:
            candidate = get_nested_url(payload, chain)
            if candidate:
                return candidate
        return None

    def eleven_creative_extract_status(payload: dict) -> str:
        if not isinstance(payload, dict):
            return ""
        raw = (
            payload.get("status")
            or payload.get("state")
            or ((payload.get("data") or {}).get("status") if isinstance(payload.get("data"), dict) else None)
            or ((payload.get("result") or {}).get("status") if isinstance(payload.get("result"), dict) else None)
            or ""
        )
        return str(raw).strip().lower()

    def eleven_creative_submit_generation(
        template_name: str,
        file_name: str,
        prompt: str,
        input_path: str,
    ) -> tuple[str | None, str | None]:
        generate_path = (app.config.get("ELEVENLABS_CREATIVE_GENERATE_PATH") or "").strip()
        if not generate_path:
            raise RuntimeError("ELEVENLABS_CREATIVE_GENERATE_PATH не задан.")
        model_id = (app.config.get("ELEVENLABS_CREATIVE_MODEL_ID") or "").strip()
        client = elevenlabs_sdk_preflight()
        sdk_http = client._client_wrapper.httpx_client

        payload = {"prompt": prompt}
        if model_id:
            payload["model_id"] = model_id

        last_error = None

        # Attempt 1: multipart upload with local video file.
        try:
            with open(input_path, "rb") as video_file:
                files = {"video": (os.path.basename(input_path), video_file)}
                resp = sdk_http.request(
                    path=generate_path.lstrip("/"),
                    method="POST",
                    headers={"Accept": "application/json"},
                    data=payload,
                    files=files,
                )
            resp.raise_for_status()
            data = resp.json() if resp.content else {}
            output_url = eleven_creative_extract_output_url(data)
            generation_id = eleven_creative_extract_generation_id(data)
            return generation_id, output_url
        except Exception as exc:
            last_error = exc

        # Attempt 2: JSON payload with public URL to source clip.
        server = (app.config.get("SERVER") or "").rstrip("/")
        if server:
            source_url = f"{server}/static/templates2/{template_name}/{file_name}"
            json_payload = dict(payload)
            json_payload["video_url"] = source_url
            try:
                resp = sdk_http.request(
                    path=generate_path.lstrip("/"),
                    method="POST",
                    headers={"Accept": "application/json"},
                    json=json_payload,
                )
                resp.raise_for_status()
                data = resp.json() if resp.content else {}
                output_url = eleven_creative_extract_output_url(data)
                generation_id = eleven_creative_extract_generation_id(data)
                return generation_id, output_url
            except Exception as exc:
                last_error = exc

        if last_error is not None:
            message = str(last_error)
            if isinstance(last_error, requests.HTTPError) and last_error.response is not None:
                message = f"{message}; body={last_error.response.text}"
            raise RuntimeError(f"Ошибка запуска ElevenLabs Creative: {message}")
        raise RuntimeError("Не удалось запустить ElevenLabs Creative генерацию.")

    def eleven_creative_poll_result(generation_id: str) -> str:
        status_template = (app.config.get("ELEVENLABS_CREATIVE_STATUS_PATH") or "").strip()
        if "{generation_id}" in status_template:
            status_path = status_template.replace("{generation_id}", generation_id)
        elif status_template:
            status_path = f"{status_template.rstrip('/')}/{generation_id}"
        else:
            status_path = f"/v1/creative/image-video/{generation_id}"

        client = elevenlabs_sdk_preflight()
        sdk_http = client._client_wrapper.httpx_client
        max_attempts = max(1, int(app.config.get("ELEVENLABS_CREATIVE_POLL_MAX_ATTEMPTS", 60)))
        interval = max(1, int(app.config.get("ELEVENLABS_CREATIVE_POLL_INTERVAL_SECONDS", 5)))
        last_payload = None

        for _ in range(max_attempts):
            resp = sdk_http.request(path=status_path.lstrip("/"), method="GET", headers={"Accept": "application/json"})
            resp.raise_for_status()
            data = resp.json() if resp.content else {}
            last_payload = data
            output_url = eleven_creative_extract_output_url(data)
            if output_url:
                return output_url

            status = eleven_creative_extract_status(data)
            if status in {"failed", "error", "cancelled", "canceled"}:
                raise RuntimeError(f"ElevenLabs Creative завершил задачу с ошибкой: {data}")
            if status in {"completed", "done", "succeeded", "success", "ready"}:
                raise RuntimeError(f"ElevenLabs Creative вернул '{status}', но без output URL: {data}")

            time.sleep(interval)

        raise RuntimeError(f"Таймаут ожидания результата ElevenLabs Creative. Последний ответ: {last_payload}")

    def run_eleven_creative_modify_job(template_name: str, file_name: str, prompt: str) -> str:
        template_dir = safe_template2_dir(template_name)
        if not template_dir:
            raise RuntimeError("Папка шаблона 2 не найдена.")

        input_path = os.path.join(template_dir, file_name)
        if not os.path.isfile(input_path):
            raise RuntimeError("Файл фрагмента не найден.")

        update_template_job(
            template_dir,
            file_name,
            status="processing",
            message="Запускаю задачу в ElevenLabs Creative…",
            prompt=prompt,
            event_name="template2_job_update",
            room_prefix="template2",
        )

        generation_id, output_url = eleven_creative_submit_generation(template_name, file_name, prompt, input_path)
        if not output_url:
            update_template_job(
                template_dir,
                file_name,
                status="processing",
                message="Ожидаю готовность видео в ElevenLabs Creative…",
                event_name="template2_job_update",
                room_prefix="template2",
            )
            if not generation_id:
                raise RuntimeError("ElevenLabs Creative не вернул generation_id.")
            output_url = eleven_creative_poll_result(generation_id)

        update_template_job(
            template_dir,
            file_name,
            status="processing",
            message="Скачиваю готовое видео…",
            event_name="template2_job_update",
            room_prefix="template2",
        )

        processed_dir = os.path.join(template_dir, "processed")
        os.makedirs(processed_dir, exist_ok=True)
        output_name = f"{os.path.splitext(file_name)[0]}__elevencreative_{int(time.time())}.mp4"
        output_path = os.path.join(processed_dir, output_name)
        out = requests.get(output_url, timeout=180)
        out.raise_for_status()
        with open(output_path, "wb") as f:
            f.write(out.content)

        remux_original_audio_into_video(input_path, output_path)
        return f"/static/templates2/{template_name}/processed/{output_name}"

    def submit_template4_veo3_generation(template_name: str, scene: dict, status_hook=None) -> str:
        model_name = (scene.get("kling_model") or app.config.get("KLING_MODEL") or "").strip() or "kling-v2.6-std"
        prompt = str(scene.get("prompt") or "").strip()
        if not prompt:
            raise RuntimeError("Промпт сцены пуст.")
        template_dir = safe_template4_dir(template_name)
        if not template_dir:
            raise RuntimeError("Папка шаблона 4 не найдена.")
        image_ref = str(scene.get("image_ref") or "").strip()
        if not image_ref:
            raise RuntimeError("Для сцены не задана картинка первого кадра.")
        image_path = os.path.abspath(os.path.join(template_dir, image_ref))
        if not os.path.isfile(image_path):
            raise RuntimeError("Файл картинки сцены не найден.")
        image_url = to_public_asset_url(template_name, image_ref)
        with open(image_path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode("ascii")
        image_encoding = (app.config.get("KLING_IMAGE_ENCODING") or "url").strip().lower()
        if image_encoding not in {"url", "base64"}:
            image_encoding = "url"
        if image_encoding == "url" and not image_url:
            raise RuntimeError("Не удалось подготовить публичный URL для image.")

        max_seconds = int(app.config.get("TEMPLATE4_MAX_VIDEO_SECONDS", 10))
        duration = max(1, min(max_seconds, int(scene.get("duration_seconds") or 8)))
        duration = 10 if duration > 5 else 5  # Kling models typically support 5 or 10 sec
        full_prompt = prompt
        mode_raw = (app.config.get("KLING_MODE") or "standard").strip().lower()
        mode = "pro" if mode_raw in {"pro", "professional"} else "std"
        enable_audio = bool(app.config.get("KLING_ENABLE_AUDIO", True))
        aspect_ratio = (app.config.get("KLING_ASPECT_RATIO") or "16:9").strip()
        base_url = (app.config.get("KLING_BASE_URL") or "").rstrip("/")
        if not base_url:
            raise RuntimeError("KLING_BASE_URL не задан.")
        path = (app.config.get("KLING_IMAGE2VIDEO_PATH") or "/v1/videos/image2video").strip()
        submit_url = f"{base_url}{path if path.startswith('/') else '/' + path}"

        def build_payload(encoding: str, use_model_name: bool) -> dict:
            image_value = image_b64 if encoding == "base64" else image_url
            payload = {
                "prompt": full_prompt,
                "image": image_value,
                "duration": duration,
                "aspect_ratio": aspect_ratio,
                "mode": mode,
                "sound": "on" if enable_audio else "off",
                "enable_audio": bool(enable_audio),
            }
            audio_ref = str(scene.get("audio_ref") or "").strip()
            if audio_ref:
                audio_url = to_public_asset_url(template_name, audio_ref)
                if audio_url:
                    payload["audio"] = audio_url
                    payload["audio_url"] = audio_url
            if use_model_name:
                payload["model_name"] = model_name
            else:
                payload["model"] = model_name
            return {k: v for k, v in payload.items() if v is not None}

        encodings = [image_encoding, "base64" if image_encoding == "url" else "url"]
        payload_candidates = [
            build_payload(encodings[0], False),
            build_payload(encodings[0], True),
            build_payload(encodings[1], False),
            build_payload(encodings[1], True),
        ]

        max_retries = max(0, int(app.config.get("TEMPLATE4_SUBMIT_MAX_RETRIES", 4)))
        retry_base = max(1, int(app.config.get("TEMPLATE4_SUBMIT_RETRY_BASE_SECONDS", 6)))
        resp = None
        last_err = None
        for payload_idx, payload in enumerate(payload_candidates, start=1):
            for attempt in range(max_retries + 1):
                try:
                    if callable(status_hook):
                        status_hook(f"Отправка в KLING... формат {payload_idx}/{len(payload_candidates)}, попытка {attempt + 1}/{max_retries + 1}")
                    resp = kling_request("POST", submit_url, json_body=payload, timeout=30, allow_429_retry=False)
                    if resp.status_code == 429:
                        retry_after_raw = (resp.headers.get("Retry-After") or "").strip()
                        raise RuntimeError(
                            "KLING вернул 429 (Too Many Requests). "
                            f"{'Retry-After=' + retry_after_raw + ' сек. ' if retry_after_raw else ''}"
                            "Это лимит/квота аккаунта, а не ошибка формата запроса."
                        )
                    if resp.status_code in (400, 404, 422) and attempt == 0:
                        if callable(status_hook):
                            body = (resp.text or "")[:240]
                            status_hook(f"KLING отклонил формат {payload_idx}: HTTP {resp.status_code}. Пробую альтернативу...")
                        break
                    resp.raise_for_status()
                    app.logger.info("Template4 KLING submit accepted template=%s scene=%s payload_variant=%s status=%s", template_name, scene.get("id"), payload_idx, resp.status_code)
                    break
                except Exception as exc:
                    last_err = exc
                    app.logger.warning("Template4 KLING submit error template=%s scene=%s payload_variant=%s attempt=%s: %s", template_name, scene.get("id"), payload_idx, attempt + 1, exc)
                    if attempt >= max_retries:
                        break
                    time.sleep(retry_base * (attempt + 1))
            if resp is not None and resp.ok:
                break
        if resp is None:
            raise RuntimeError(f"Не удалось отправить KLING задачу: {last_err}")
        if resp.status_code == 429:
            raise RuntimeError(
                "KLING API вернул 429 (Too Many Requests). "
                "Превышен лимит запросов/квоты. Подождите 1-2 минуты и попробуйте снова."
            )
        data = resp.json() if resp.content else {}
        task_id = (
            data.get("task_id")
            or (data.get("data") or {}).get("task_id")
            or (data.get("task_info") or {}).get("id")
            or (data.get("data") or {}).get("taskId")
        )
        if not task_id:
            raise RuntimeError(f"KLING submit не вернул task_id: {data}")
        app.logger.info("Template4 KLING task created template=%s scene=%s task_id=%s", template_name, scene.get("id"), task_id)
        if callable(status_hook):
            status_hook(f"KLING принял задачу: {task_id}. Ожидаю результат...")
        return str(task_id)

    def poll_template4_veo3_result(operation_name: str, status_hook=None) -> str:
        poll_attempts = max(1, int(app.config.get("TEMPLATE4_POLL_MAX_ATTEMPTS", 180)))
        poll_interval = max(1, int(app.config.get("TEMPLATE4_POLL_INTERVAL_SECONDS", 5)))
        last_payload = None
        base_url = (app.config.get("KLING_BASE_URL") or "").rstrip("/")
        status_path_tpl = (app.config.get("KLING_TASK_STATUS_PATH") or "/v1/videos/{task_id}").strip()
        status_candidates = [
            status_path_tpl.replace("{task_id}", operation_name),
            f"/v1/videos/image2video/{operation_name}",
            f"/v1/videos/{operation_name}",
        ]
        status_urls = []
        for p in status_candidates:
            path = p if p.startswith("/") else f"/{p}"
            status_urls.append(f"{base_url}{path}")
        for idx in range(1, poll_attempts + 1):
            data = None
            for status_url in status_urls:
                try:
                    resp = kling_request("GET", status_url, json_body=None, timeout=30, allow_429_retry=False)
                    if resp.status_code == 429:
                        retry_after_raw = (resp.headers.get("Retry-After") or "").strip()
                        try:
                            wait_sec = max(3, int(retry_after_raw))
                        except Exception:
                            wait_sec = max(3, poll_interval * 3)
                        if callable(status_hook):
                            status_hook(f"KLING rate limit на статусе (429). Жду {wait_sec} сек...")
                        time.sleep(wait_sec)
                        continue
                    if resp.status_code == 404:
                        continue
                    if resp.status_code >= 400:
                        app.logger.warning(
                            "Template4 KLING status HTTP %s for task=%s url=%s body=%s",
                            resp.status_code,
                            operation_name,
                            status_url,
                            (resp.text or "")[:400],
                        )
                        continue
                    resp.raise_for_status()
                    data = resp.json() if resp.content else {}
                    break
                except Exception as exc:
                    app.logger.warning("Template4 KLING status request error task=%s url=%s: %s", operation_name, status_url, exc)
                    continue
            if data is None:
                if callable(status_hook):
                    status_hook(f"Ожидание статуса KLING... попытка {idx}/{poll_attempts}")
                time.sleep(poll_interval)
                continue
            last_payload = data
            status = str(
                data.get("status")
                or data.get("task_status")
                or (data.get("task_info") or {}).get("status")
                or (data.get("task_info") or {}).get("task_status")
                or (data.get("data") or {}).get("status")
                or (data.get("data") or {}).get("task_status")
                or (data.get("data") or {}).get("progress_status")
                or ""
            ).lower()
            if callable(status_hook):
                status_hook(f"KLING статус: {status or 'unknown'} ({idx}/{poll_attempts})")
            if status in {"succeed", "succeeded", "completed", "success", "done", "finished"}:
                candidates = [
                    data.get("url"),
                    data.get("video_url"),
                    (data.get("data") or {}).get("url"),
                    (data.get("data") or {}).get("video_url"),
                    (data.get("data") or {}).get("result_url"),
                    (data.get("task_result") or {}).get("url") if isinstance(data.get("task_result"), dict) else None,
                    (data.get("task_result") or {}).get("video_url") if isinstance(data.get("task_result"), dict) else None,
                    (
                        (((data.get("data") or {}).get("task_result") or {}).get("videos") or [None])[0].get("url")
                        if isinstance((((data.get("data") or {}).get("task_result") or {}).get("videos") or [None])[0], dict)
                        else None
                    ),
                    (data.get("output") or {}).get("url") if isinstance(data.get("output"), dict) else None,
                    (data.get("output") or {}).get("video_url") if isinstance(data.get("output"), dict) else None,
                    ((data.get("videos") or [None])[0] if isinstance(data.get("videos"), list) else None),
                    ((data.get("data") or {}).get("videos") or [None])[0] if isinstance((data.get("data") or {}).get("videos"), list) else None,
                ]
                for c in candidates:
                    if isinstance(c, dict):
                        c = c.get("url") or c.get("video_url") or ""
                    if isinstance(c, str) and c.startswith(("http://", "https://")):
                        return c
                raise RuntimeError(f"KLING вернул completed, но без URL видео: {data}")
            if status in {"submitted", "queued", "in_progress", "processing", "running", "pending"}:
                time.sleep(poll_interval)
                continue
            if status in {"failed", "error", "failure", "canceled", "cancelled", "timeout"}:
                raise RuntimeError(f"KLING задача завершилась ошибкой: {data}")
            time.sleep(poll_interval)
        raise RuntimeError(f"Таймаут ожидания KLING результата. Последний ответ: {last_payload}")

    def run_template4_veo3_job(template_name: str, scene: dict, status_hook=None) -> str:
        operation_name = str(scene.get("kling_task_id") or "").strip()
        if operation_name:
            if callable(status_hook):
                status_hook(f"Продолжаю ожидание KLING задачи после перезапуска: {operation_name}")
        else:
            operation_name = submit_template4_veo3_generation(template_name, scene, status_hook=status_hook)
            scene["kling_task_id"] = operation_name
        if callable(status_hook):
            status_hook("Задача KLING отправлена. Ожидаю рендер...")
        video_uri = poll_template4_veo3_result(operation_name, status_hook=status_hook)
        template_dir = safe_template4_dir(template_name)
        if not template_dir:
            raise RuntimeError("Папка шаблона 4 не найдена.")
        processed_dir = os.path.join(template_dir, "processed")
        os.makedirs(processed_dir, exist_ok=True)
        run_suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        step_part = str(scene.get("__step_id") or "").strip()
        out_name = (
            f"scene_{scene.get('id', 'unknown')}__step_{step_part}__veo_{run_suffix}.mp4"
            if step_part
            else f"scene_{scene.get('id', 'unknown')}__veo_{run_suffix}.mp4"
        )
        out_path = os.path.join(processed_dir, out_name)
        dl = requests.get(video_uri, timeout=180, allow_redirects=True)
        dl.raise_for_status()
        with open(out_path, "wb") as f:
            f.write(dl.content)
        # For Template4 step-based flow, audio from step settings is mandatory output behavior.
        # KLING may ignore external audio, so enforce it by muxing selected audio locally.
        audio_ref = str(scene.get("audio_ref") or "").strip()
        if audio_ref:
            audio_path = os.path.abspath(os.path.join(template_dir, audio_ref))
            if not os.path.isfile(audio_path):
                raise RuntimeError(f"Выбранный аудиофайл не найден: {audio_ref}")
            if not ffmpeg_available():
                raise RuntimeError("ffmpeg не найден: не могу добавить обязательное аудио в видео.")
            muxed_path = out_path.replace(".mp4", "__with_audio.mp4")
            if callable(status_hook):
                status_hook("Добавляю выбранное аудио к сгенерированному видео...")
            cmd = [
                "ffmpeg", "-y",
                "-i", out_path,
                "-i", audio_path,
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-c:v", "copy",
                "-c:a", "aac",
                "-shortest",
                muxed_path,
            ]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if proc.returncode != 0 or not os.path.isfile(muxed_path):
                raise RuntimeError(f"Не удалось вставить аудио в видео: {(proc.stderr or proc.stdout or '').strip()[:300]}")
            os.replace(muxed_path, out_path)
        return f"/static/templates4/{template_name}/processed/{out_name}"

    def run_template4_scene_step(template_name: str, scene: dict, step: dict, status_hook=None) -> str:
        step_type = str(step.get("type") or "").strip()
        step_prompt = (step.get("prompt") or scene.get("prompt") or "").strip()
        template_dir = safe_template4_dir(template_name)
        if not template_dir:
            raise RuntimeError("Папка шаблона 4 не найдена.")
        assets_dir = os.path.join(template_dir, "assets")
        os.makedirs(assets_dir, exist_ok=True)

        def scene_image_abs_from_ref(ref: str) -> str:
            v = (ref or "").strip()
            if not v:
                return ""
            p = os.path.abspath(os.path.join(template_dir, v))
            return p if os.path.isfile(p) else ""

        def latest_image_ref_for_scene() -> str:
            steps = scene.get("steps") if isinstance(scene.get("steps"), list) else []
            for st in reversed(steps):
                if str(st.get("status") or "").lower() != "completed":
                    continue
                out = str(st.get("output_url") or "").strip()
                if out.startswith(f"/static/templates4/{template_name}/") and out.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                    return out.replace(f"/static/templates4/{template_name}/", "", 1)
            return str(scene.get("image_ref") or "").strip()

        if step_type in {"edit_first_frame", "add_avatar_to_first_frame", "openai_image_edit", "openai_insert_avatar"}:
            if not app.config.get("OPENAI_API_KEY"):
                raise RuntimeError("OPENAI_API_KEY не задан в .env.")
            if callable(status_hook):
                status_hook("OpenAI: подготавливаю входные изображения...")
            image_ref = str(step.get("source_image_ref") or "").strip() or latest_image_ref_for_scene()
            if not image_ref:
                raise RuntimeError("У сцены нет исходной картинки.")
            image_path = scene_image_abs_from_ref(image_ref)
            if not image_path:
                raise RuntimeError("Исходная картинка сцены не найдена.")
            avatar_ref = str(step.get("avatar_url") or scene.get("avatar_url") or "").strip()
            openai_model = (step.get("model") or app.config["OPENAI_AVATAR_MODEL"] or "").strip()
            with open(image_path, "rb") as base_img:
                if step_type in {"openai_insert_avatar", "add_avatar_to_first_frame"}:
                    if not avatar_ref:
                        raise RuntimeError("Не задан аватар ребенка для шага вставки в кадр.")
                    if callable(status_hook):
                        status_hook("OpenAI: загружаю аватар и вставляю его в выбранный кадр...")
                    avatar_input_ref = avatar_ref if avatar_ref.startswith("http") else (f"{app.config.get('SERVER','').rstrip('/')}{avatar_ref}" if avatar_ref.startswith("/") else avatar_ref)
                    avatar_bytes = image_ref_to_bytes(avatar_input_ref)
                    avatar_tmp_path = ""
                    try:
                        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as avatar_tmp:
                            avatar_tmp.write(avatar_bytes)
                            avatar_tmp_path = avatar_tmp.name
                        with open(avatar_tmp_path, "rb") as avatar_img:
                            insert_prompt = (
                                "Main task: place the child avatar from the second input image into the first input frame. "
                                "Keep the first frame scene/composition as primary. "
                                "The avatar must be clearly visible and naturally integrated (size, pose, lighting, perspective). "
                                "Do not replace the scene with avatar-only image. "
                                f"Additional instruction: {step_prompt or 'no extra instruction'}"
                            )
                            result = openai_client().images.edit(
                                model=openai_model,
                                image=[base_img, avatar_img],
                                prompt=insert_prompt,
                                size=app.config["OPENAI_AVATAR_SIZE"],
                            )
                    except Exception as exc:
                        raise RuntimeError(f"Не удалось вставить аватар в кадр через OpenAI: {exc}") from exc
                    finally:
                        if avatar_tmp_path:
                            try:
                                os.remove(avatar_tmp_path)
                            except Exception:
                                pass
                else:
                    if callable(status_hook):
                        status_hook("OpenAI: запускаю редактирование кадра...")
                    result = openai_client().images.edit(
                        model=openai_model,
                        image=base_img,
                        prompt=step_prompt,
                        size=app.config["OPENAI_AVATAR_SIZE"],
                    )
            data = getattr(result, "data", None) or []
            if not data:
                raise RuntimeError("OpenAI Images не вернул результат.")
            first = data[0]
            b64_json = getattr(first, "b64_json", None) or (first.get("b64_json") if isinstance(first, dict) else None)
            image_url = getattr(first, "url", None) or (first.get("url") if isinstance(first, dict) else None)
            run_suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
            out_name = f"scene_{scene.get('id')}_step_{step.get('id')}_{run_suffix}.png"
            out_path = os.path.join(assets_dir, out_name)
            if b64_json:
                if callable(status_hook):
                    status_hook("OpenAI: сохраняю результат шага (base64)...")
                with open(out_path, "wb") as f:
                    f.write(base64.b64decode(b64_json))
            elif image_url:
                if callable(status_hook):
                    status_hook("OpenAI: скачиваю результат шага по URL...")
                dl = requests.get(image_url, timeout=60)
                dl.raise_for_status()
                with open(out_path, "wb") as f:
                    f.write(dl.content)
            else:
                raise RuntimeError("OpenAI не вернул URL/base64 изображения.")
            return f"/static/templates4/{template_name}/assets/{out_name}"

        if step_type in {"generate_video", "kling_video"}:
            source_image_ref = str(step.get("source_image_ref") or "").strip()
            if not source_image_ref:
                raise RuntimeError("В шаге generate_video не выбран первый кадр (source_image_ref).")
            source_audio_ref = str(step.get("source_audio_ref") or "").strip()
            scene_for_video = {
                "id": scene.get("id"),
                "prompt": step_prompt or str(scene.get("prompt") or "").strip(),
                "image_ref": source_image_ref,
                "audio_ref": source_audio_ref,
                "duration_seconds": int(scene.get("duration_seconds") or 8),
            }
            step_model = str(step.get("model") or "").strip()
            if step_model:
                scene_for_video["kling_model"] = step_model
            scene_for_video["__step_id"] = str(step.get("id") or "").strip()
            if callable(status_hook):
                status_hook(
                    f"Kling: запускаю генерацию видео шага (кадр={source_image_ref}, аудио={source_audio_ref or 'без аудио'})..."
                )
            return run_template4_veo3_job(template_name, scene_for_video, status_hook=status_hook)

        raise RuntimeError(f"Неизвестный тип шага: {step_type}")

    def sd_webui_headers() -> dict:
        headers = {"Content-Type": "application/json"}
        api_key = (app.config.get("SD_WEBUI_API_KEY") or "").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def resolve_template3_source_face(template_dir: str, source_face_ref: str) -> bytes:
        ref = (source_face_ref or "").strip()
        if not ref:
            raise ValueError("Укажите source face (URL или имя файла в папке шаблона).")
        if ref.startswith("http://") or ref.startswith("https://"):
            resp = requests.get(ref, timeout=60)
            resp.raise_for_status()
            return resp.content
        source_path = os.path.abspath(os.path.join(template_dir, ref))
        template_dir_abs = os.path.abspath(template_dir)
        if os.path.commonpath([template_dir_abs, source_path]) != template_dir_abs:
            raise ValueError("Недопустимый путь source face.")
        if not os.path.isfile(source_path):
            raise ValueError("Файл source face не найден.")
        with open(source_path, "rb") as f:
            return f.read()

    def run_sd_character_replace_job(
        template_name: str,
        file_name: str,
        prompt: str,
        source_face_ref: str,
        child_avatar_id: int | None,
        negative_prompt: str,
        denoise_strength: float | None,
    ) -> str:
        template_dir = safe_template3_dir(template_name)
        if not template_dir:
            raise ValueError("Шаблон 3 не найден.")
        input_path = os.path.join(template_dir, file_name)
        if not os.path.isfile(input_path):
            raise ValueError("Входной видеофайл не найден.")
        if os.path.splitext(file_name)[1].lstrip(".").lower() not in {"mp4", "mov", "webm", "m4v", "avi", "mkv"}:
            raise ValueError("В Шаблоны 3 поддерживаются только видеофайлы.")

        update_template_job(
            template_dir,
            file_name,
            status="processing",
            message="Подготавливаю source-face и извлекаю кадры…",
            event_name="template3_job_update",
            room_prefix="template3",
        )
        source_face_bytes = None
        if child_avatar_id:
            avatar = CartoonAvatar.query.get(int(child_avatar_id))
            if not avatar:
                raise ValueError("Выбранный аватар ребёнка не найден.")
            if avatar.status != "completed":
                raise ValueError("Выбранный аватар ребёнка ещё не готов.")
            image_ref = (avatar.image_url or "").strip()
            if not image_ref or (image_ref.startswith("http") and is_expired_signed_url(image_ref)):
                image_ref = refresh_avatar_url_if_possible(avatar) or ""
            candidate = build_avatar_image_candidate(image_ref)
            if not candidate:
                raise ValueError("Выбранный аватар ребёнка недоступен.")
            if candidate.get("kind") == "file":
                with open(candidate.get("value", ""), "rb") as f:
                    source_face_bytes = f.read()
            else:
                resp = requests.get(candidate.get("value", ""), timeout=60)
                resp.raise_for_status()
                source_face_bytes = resp.content
        if source_face_bytes is None:
            source_face_bytes = resolve_template3_source_face(template_dir, source_face_ref)
        source_face_b64 = base64.b64encode(source_face_bytes).decode("utf-8")

        sd_base = (app.config.get("SD_WEBUI_BASE_URL") or "").rstrip("/")
        if not sd_base:
            raise ValueError("SD_WEBUI_BASE_URL не задан.")
        denoise = denoise_strength if denoise_strength is not None else float(app.config.get("SD_DENOISE_STRENGTH", 0.55))
        face_script_name = (app.config.get("SD_FACE_SCRIPT_NAME") or "reactor").strip()
        use_face_script = bool(face_script_name)
        if use_face_script:
            try:
                scripts_resp = requests.get(
                    f"{sd_base}/sdapi/v1/scripts",
                    headers=sd_webui_headers(),
                    timeout=30,
                )
                scripts_resp.raise_for_status()
                scripts_data = scripts_resp.json() or {}
                txt2img_scripts = [str(x).strip().lower() for x in (scripts_data.get("txt2img") or [])]
                img2img_scripts = [str(x).strip().lower() for x in (scripts_data.get("img2img") or [])]
                available_scripts = set(txt2img_scripts + img2img_scripts)
                if face_script_name.strip().lower() not in available_scripts:
                    use_face_script = False
                    update_template_job(
                        template_dir,
                        file_name,
                        status="processing",
                        message=f"Скрипт '{face_script_name}' не найден в SD WebUI. Продолжаю без face-swap скрипта.",
                        event_name="template3_job_update",
                        room_prefix="template3",
                    )
            except Exception:
                # If scripts list cannot be fetched, keep current behavior and try direct call.
                pass

        with tempfile.TemporaryDirectory(prefix="sd_video_") as tmp_dir:
            frames_in = os.path.join(tmp_dir, "frames_in")
            frames_out = os.path.join(tmp_dir, "frames_out")
            os.makedirs(frames_in, exist_ok=True)
            os.makedirs(frames_out, exist_ok=True)

            subprocess.run(
                ["ffmpeg", "-y", "-i", input_path, "-vsync", "0", os.path.join(frames_in, "frame_%06d.png")],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            frame_files = sorted([f for f in os.listdir(frames_in) if f.lower().endswith(".png")])
            if not frame_files:
                raise RuntimeError("Не удалось извлечь кадры из видео.")

            total = len(frame_files)
            for idx, frame_name in enumerate(frame_files, start=1):
                frame_path = os.path.join(frames_in, frame_name)
                with open(frame_path, "rb") as f:
                    frame_bytes = f.read()
                frame_b64 = base64.b64encode(frame_bytes).decode("utf-8")

                with Image.open(BytesIO(frame_bytes)) as img:
                    width, height = img.size

                alwayson = {}
                controlnet_model = (app.config.get("SD_CONTROLNET_MODEL") or "").strip()
                if controlnet_model:
                    alwayson["controlnet"] = {
                        "args": [
                            {
                                "enabled": True,
                                "input_image": frame_b64,
                                "module": app.config.get("SD_CONTROLNET_MODULE", "none"),
                                "model": controlnet_model,
                                "weight": float(app.config.get("SD_CONTROLNET_WEIGHT", 0.9)),
                                "resize_mode": "Scale to Fit (Inner Fit)",
                                "processor_res": max(width, height),
                            }
                        ]
                    }
                if use_face_script and face_script_name:
                    alwayson[face_script_name] = {
                        "args": [
                            {
                                "enabled": True,
                                "source_image": source_face_b64,
                            }
                        ]
                    }
                payload = {
                    "init_images": [frame_b64],
                    "prompt": prompt,
                    "negative_prompt": negative_prompt or "",
                    "denoising_strength": denoise,
                    "steps": int(app.config.get("SD_STEPS", 24)),
                    "cfg_scale": float(app.config.get("SD_CFG_SCALE", 6.0)),
                    "sampler_name": app.config.get("SD_SAMPLER_NAME", "DPM++ 2M Karras"),
                    "width": width,
                    "height": height,
                    "batch_size": 1,
                    "n_iter": 1,
                    "override_settings": {},
                }
                if alwayson:
                    payload["alwayson_scripts"] = alwayson
                checkpoint = (app.config.get("SD_MODEL_CHECKPOINT") or "").strip()
                if checkpoint:
                    payload["override_settings"]["sd_model_checkpoint"] = checkpoint
                if not payload["override_settings"]:
                    payload.pop("override_settings")

                frame_retries = max(0, int(app.config.get("SD_FRAME_RETRIES", 2)))
                connect_timeout = max(5, int(app.config.get("SD_CONNECT_TIMEOUT_SECONDS", 30)))
                read_timeout_raw = int(app.config.get("SD_READ_TIMEOUT_SECONDS", 0))
                read_timeout = None if read_timeout_raw <= 0 else max(60, read_timeout_raw)
                request_timeout = (connect_timeout, read_timeout)
                resp = None
                last_exc = None
                for attempt in range(1, frame_retries + 2):
                    try:
                        resp = requests.post(
                            f"{sd_base}/sdapi/v1/img2img",
                            headers=sd_webui_headers(),
                            json=payload,
                            timeout=request_timeout,
                        )
                        last_exc = None
                        break
                    except requests.exceptions.ReadTimeout as exc:
                        last_exc = exc
                        if attempt <= frame_retries:
                            update_template_job(
                                template_dir,
                                file_name,
                                status="processing",
                                message=(
                                    f"SD timeout на кадре {idx}/{total}, "
                                    f"повтор {attempt}/{frame_retries}..."
                                ),
                                event_name="template3_job_update",
                                room_prefix="template3",
                            )
                            time.sleep(min(10, 2 * attempt))
                            continue
                        raise
                if last_exc:
                    raise last_exc
                if resp is None:
                    raise RuntimeError("SD img2img: пустой ответ после повторов.")
                if resp.status_code >= 400:
                    err_text = (resp.text or "").strip()
                    if len(err_text) > 1200:
                        err_text = err_text[:1200] + "..."
                    raise RuntimeError(
                        f"SD img2img error {resp.status_code}: {err_text or 'empty response body'}"
                    )
                data = resp.json() or {}
                images = data.get("images") or []
                if not images:
                    raise RuntimeError(f"SD API вернул пустой результат на кадре {idx}/{total}.")
                out_bytes = base64.b64decode(images[0].split(",", 1)[-1])
                with open(os.path.join(frames_out, frame_name), "wb") as f:
                    f.write(out_bytes)

                if idx == 1 or idx % 10 == 0 or idx == total:
                    update_template_job(
                        template_dir,
                        file_name,
                        status="processing",
                        message=f"Stable Diffusion: обработано кадров {idx}/{total}",
                        event_name="template3_job_update",
                        room_prefix="template3",
                    )

            fps = probe_video_timing(input_path)[1] or 25.0
            processed_dir = os.path.join(template_dir, "processed")
            os.makedirs(processed_dir, exist_ok=True)
            output_name = f"{os.path.splitext(file_name)[0]}__sd_{int(time.time())}.mp4"
            output_path = os.path.join(processed_dir, output_name)
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-framerate",
                    str(fps),
                    "-i",
                    os.path.join(frames_out, "frame_%06d.png"),
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    output_path,
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            remux_original_audio_into_video(input_path, output_path)
        return f"/static/templates3/{template_name}/processed/{output_name}"

    def enqueue_template2_job(template_name: str, file_name: str, prompt: str) -> None:
        ensure_template2_worker_started()
        job = {
            "job_id": uuid.uuid4().hex,
            "template_name": template_name,
            "file_name": file_name,
            "prompt": prompt,
            "queued_at": datetime.now(timezone.utc).isoformat(),
        }
        client = redis_client()
        queue_name = app.config["ELEVENLABS_CREATIVE_QUEUE_NAME"]
        queue_size = client.lpush(queue_name, json.dumps(job, ensure_ascii=False))
        app.logger.info(
            "Enqueued Template2 job id=%s template=%s file=%s queue=%s size=%s",
            job["job_id"],
            template_name,
            file_name,
            queue_name,
            queue_size,
        )
        try:
            socketio.start_background_task(template2_queue_worker_once)
        except Exception:
            pass

    def enqueue_template3_job(
        template_name: str,
        file_name: str,
        prompt: str,
        source_face_ref: str,
        child_avatar_id: int | None,
        negative_prompt: str,
        denoise_strength: float | None,
    ) -> None:
        ensure_template3_worker_started()
        job = {
            "job_id": uuid.uuid4().hex,
            "template_name": template_name,
            "file_name": file_name,
            "prompt": prompt,
            "source_face_ref": source_face_ref,
            "child_avatar_id": child_avatar_id,
            "negative_prompt": negative_prompt,
            "denoise_strength": denoise_strength,
            "queued_at": datetime.now(timezone.utc).isoformat(),
        }
        client = redis_client()
        queue_name = app.config["SD_VIDEO_QUEUE_NAME"]
        queue_size = client.lpush(queue_name, json.dumps(job, ensure_ascii=False))
        app.logger.info(
            "Enqueued Template3 job id=%s template=%s file=%s queue=%s size=%s",
            job["job_id"],
            template_name,
            file_name,
            queue_name,
            queue_size,
        )
        try:
            socketio.start_background_task(template3_queue_worker_once)
        except Exception:
            pass

    def process_template2_job_payload(payload: dict) -> None:
        template_name = payload.get("template_name", "")
        file_name = payload.get("file_name", "")
        prompt = payload.get("prompt", "")
        job_id = payload.get("job_id", "")
        with app.app_context():
            template_dir = safe_template2_dir(template_name)
            if not template_dir:
                app.logger.warning("Template2 job skipped: template not found job=%s template=%s", job_id, template_name)
                return
            app.logger.info(
                "Template2 worker picked job id=%s template=%s file=%s",
                job_id,
                template_name,
                file_name,
            )
            try:
                output_url = run_eleven_creative_modify_job(template_name, file_name, prompt)
                update_template_job(
                    template_dir,
                    file_name,
                    status="completed",
                    message="Готово",
                    output_url=output_url,
                    event_name="template2_job_update",
                    room_prefix="template2",
                )
            except Exception as exc:
                app.logger.exception(
                    "Template2 job failed template=%s file=%s: %s",
                    template_name,
                    file_name,
                    exc,
                )
                update_template_job(
                    template_dir,
                    file_name,
                    status="failed",
                    message=str(exc),
                    event_name="template2_job_update",
                    room_prefix="template2",
                )

    def template2_queue_worker_once():
        try:
            client = redis_client()
            item = client.brpop(app.config["ELEVENLABS_CREATIVE_QUEUE_NAME"], timeout=1)
            if not item:
                return
            _, raw_payload = item
            payload = json.loads(raw_payload)
            process_template2_job_payload(payload)
        except Exception as exc:
            app.logger.error("Template2 one-shot worker error: %s", exc)

    def template2_queue_worker():
        app.logger.info("Template2 worker started")
        while True:
            try:
                client = redis_client()
                item = client.brpop(app.config["ELEVENLABS_CREATIVE_QUEUE_NAME"], timeout=5)
                if not item:
                    continue
                _, raw_payload = item
                payload = json.loads(raw_payload)
                process_template2_job_payload(payload)
            except Exception as exc:
                app.logger.error("Template2 queue worker error: %s", exc)
                time.sleep(5)

    def process_template3_job_payload(payload: dict) -> None:
        template_name = payload.get("template_name", "")
        file_name = payload.get("file_name", "")
        prompt = payload.get("prompt", "")
        source_face_ref = payload.get("source_face_ref", "")
        child_avatar_id_raw = payload.get("child_avatar_id", None)
        child_avatar_id = None
        try:
            if child_avatar_id_raw not in (None, ""):
                child_avatar_id = int(child_avatar_id_raw)
        except Exception:
            child_avatar_id = None
        negative_prompt = payload.get("negative_prompt", "")
        denoise_strength_raw = payload.get("denoise_strength", None)
        denoise_strength = None
        try:
            if denoise_strength_raw not in (None, ""):
                denoise_strength = float(denoise_strength_raw)
        except Exception:
            denoise_strength = None
        job_id = payload.get("job_id", "")
        with app.app_context():
            template_dir = safe_template3_dir(template_name)
            if not template_dir:
                app.logger.warning("Template3 job skipped: template not found job=%s template=%s", job_id, template_name)
                return
            try:
                output_url = run_sd_character_replace_job(
                    template_name,
                    file_name,
                    prompt,
                    source_face_ref,
                    child_avatar_id,
                    negative_prompt,
                    denoise_strength,
                )
                update_template_job(
                    template_dir,
                    file_name,
                    status="completed",
                    message="Готово",
                    output_url=output_url,
                    event_name="template3_job_update",
                    room_prefix="template3",
                )
            except Exception as exc:
                app.logger.exception("Template3 job failed template=%s file=%s: %s", template_name, file_name, exc)
                update_template_job(
                    template_dir,
                    file_name,
                    status="failed",
                    message=str(exc),
                    event_name="template3_job_update",
                    room_prefix="template3",
                )

    def template3_queue_worker_once():
        try:
            client = redis_client()
            item = client.brpop(app.config["SD_VIDEO_QUEUE_NAME"], timeout=1)
            if not item:
                return
            _, raw_payload = item
            payload = json.loads(raw_payload)
            process_template3_job_payload(payload)
        except Exception as exc:
            app.logger.error("Template3 one-shot worker error: %s", exc)

    def template3_queue_worker():
        app.logger.info("Template3 worker started")
        while True:
            try:
                client = redis_client()
                item = client.brpop(app.config["SD_VIDEO_QUEUE_NAME"], timeout=5)
                if not item:
                    continue
                _, raw_payload = item
                payload = json.loads(raw_payload)
                process_template3_job_payload(payload)
            except Exception as exc:
                app.logger.error("Template3 queue worker error: %s", exc)
                time.sleep(5)

    def enqueue_template4_job(template_name: str, scene_id: str, step_id: str | None = None) -> None:
        ensure_template4_worker_started()
        job = {
            "job_id": uuid.uuid4().hex,
            "template_name": template_name,
            "scene_id": scene_id,
            "step_id": step_id or "",
            "queued_at": datetime.now(timezone.utc).isoformat(),
        }
        client = redis_client()
        queue_name = app.config["TEMPLATE4_QUEUE_NAME"]
        client.lpush(queue_name, json.dumps(job, ensure_ascii=False))
        try:
            socketio.start_background_task(template4_queue_worker_once)
        except Exception:
            pass

    def process_template4_job_payload(payload: dict) -> None:
        template_name = str(payload.get("template_name") or "").strip()
        scene_id = str(payload.get("scene_id") or "").strip()
        step_id = str(payload.get("step_id") or "").strip()
        with app.app_context():
            template_dir = safe_template4_dir(template_name)
            if not template_dir or not scene_id:
                return
            scenes = load_template4_scenes(template_dir)
            target = next((s for s in scenes if str(s.get("id")) == scene_id), None)
            if not target:
                return
            if step_id:
                steps = target.get("steps") if isinstance(target.get("steps"), list) else []
                step = next((s for s in steps if str(s.get("id")) == step_id), None)
                if not step:
                    return
                step["status"] = "processing"
                step["message"] = "Запускаю шаг..."
                step["updated_at"] = datetime.now(timezone.utc).isoformat()
                save_template4_scenes(template_dir, scenes)
                try:
                    def _step_status_hook(msg: str):
                        step["status"] = "processing"
                        step["message"] = msg
                        step["updated_at"] = datetime.now(timezone.utc).isoformat()
                        save_template4_scenes(template_dir, scenes)
                        update_template_job(
                            template_dir,
                            scene_id,
                            status=str(target.get("status") or "draft"),
                            message=f"Шаг: {msg}",
                            event_name="template4_job_update",
                            room_prefix="template4",
                        )

                    output_url = run_template4_scene_step(template_name, target, step, status_hook=_step_status_hook)
                    step["status"] = "completed"
                    step["output_url"] = output_url
                    step["message"] = "Шаг выполнен"
                    step["updated_at"] = datetime.now(timezone.utc).isoformat()
                    save_template4_scenes(template_dir, scenes)
                    update_template_job(
                        template_dir,
                        scene_id,
                        status=str(target.get("status") or "draft"),
                        message=f"Шаг {step.get('name') or step_id} выполнен",
                        event_name="template4_job_update",
                        room_prefix="template4",
                    )
                except Exception as exc:
                    step["status"] = "failed"
                    step["message"] = str(exc)
                    step["updated_at"] = datetime.now(timezone.utc).isoformat()
                    save_template4_scenes(template_dir, scenes)
                    update_template_job(
                        template_dir,
                        scene_id,
                        status=str(target.get("status") or "draft"),
                        message=f"Ошибка шага: {exc}",
                        event_name="template4_job_update",
                        room_prefix="template4",
                    )
                return
            target["status"] = "processing"
            target["message"] = "Запускаю генерацию видео..."
            target["updated_at"] = datetime.now(timezone.utc).isoformat()
            save_template4_scenes(template_dir, scenes)
            update_template_job(
                template_dir,
                scene_id,
                status="processing",
                message=target["message"],
                event_name="template4_job_update",
                room_prefix="template4",
            )
            try:
                def _status_hook(msg: str):
                    target["message"] = msg
                    target["updated_at"] = datetime.now(timezone.utc).isoformat()
                    save_template4_scenes(template_dir, scenes)
                    update_template_job(
                        template_dir,
                        scene_id,
                        status="processing",
                        message=msg,
                        event_name="template4_job_update",
                        room_prefix="template4",
                    )

                output_url = run_template4_veo3_job(template_name, target, status_hook=_status_hook)

                target["status"] = "completed"
                target["video_url"] = output_url
                target["message"] = "Готово"
                target["kling_task_id"] = target.get("kling_task_id") or ""
                target["updated_at"] = datetime.now(timezone.utc).isoformat()
                save_template4_scenes(template_dir, scenes)
                update_template_job(
                    template_dir,
                    scene_id,
                    status="completed",
                    message=target["message"],
                    output_url=output_url,
                    event_name="template4_job_update",
                    room_prefix="template4",
                )
            except Exception as exc:
                target["status"] = "failed"
                target["message"] = str(exc)
                target["updated_at"] = datetime.now(timezone.utc).isoformat()
                save_template4_scenes(template_dir, scenes)
                update_template_job(
                    template_dir,
                    scene_id,
                    status="failed",
                    message=str(exc),
                    event_name="template4_job_update",
                    room_prefix="template4",
                )

    def template4_queue_worker_once():
        try:
            client = redis_client()
            item = client.brpop(app.config["TEMPLATE4_QUEUE_NAME"], timeout=1)
            if not item:
                return
            _, raw_payload = item
            payload = json.loads(raw_payload)
            process_template4_job_payload(payload)
        except Exception as exc:
            app.logger.error("Template4 one-shot worker error: %s", exc)

    def template4_queue_worker():
        app.logger.info("Template4 worker started")
        while True:
            try:
                client = redis_client()
                item = client.brpop(app.config["TEMPLATE4_QUEUE_NAME"], timeout=5)
                if not item:
                    continue
                _, raw_payload = item
                payload = json.loads(raw_payload)
                process_template4_job_payload(payload)
            except Exception as exc:
                app.logger.error("Template4 queue worker error: %s", exc)
                time.sleep(5)

    def avatar_queue_worker():
        while True:
            try:
                client = redis_client()
                item = client.brpop(app.config["AVATAR_QUEUE_NAME"], timeout=5)
                if not item:
                    continue
                _, raw_payload = item
                payload = json.loads(raw_payload)
                avatar_id = int(payload.get("avatar_id", 0) or 0)
                if not avatar_id:
                    continue
                with app.app_context():
                    avatar = CartoonAvatar.query.get(avatar_id)
                    if not avatar:
                        continue
                    child = Child.query.get(avatar.child_id)
                    if not child:
                        avatar.status = "failed"
                        db.session.commit()
                        continue

                    style = next(
                        (s for s in app.config["CARTOON_STYLES"] if s["name"] == avatar.style_name),
                        None,
                    )
                    if not style:
                        avatar.status = "failed"
                        db.session.commit()
                        socketio.emit("avatar_ready", avatar.to_dict(), room=f"child_{avatar.child_id}")
                        continue

                    ok, err = generate_avatar_with_openai(child, avatar, style)
                    if not ok:
                        avatar.status = "failed"
                        app.logger.error(
                            "Async OpenAI avatar generation failed avatar=%d child=%d style=%s: %s",
                            avatar.id,
                            avatar.child_id,
                            avatar.style_name,
                            err,
                        )
                    db.session.commit()
                    socketio.emit("avatar_ready", avatar.to_dict(), room=f"child_{avatar.child_id}")
            except Exception as exc:
                app.logger.error("Avatar queue worker error: %s", exc)
                time.sleep(5)

    def ensure_pixverse_worker_started():
        nonlocal pixverse_worker_started
        with pixverse_worker_lock:
            if pixverse_worker_started:
                return
            try:
                import redis  # noqa: F401
            except Exception:
                app.logger.warning("PixVerse worker disabled: package 'redis' is not installed")
                return
            try:
                redis_client().ping()
            except Exception as exc:
                app.logger.warning("PixVerse worker disabled: Redis unavailable (%s)", exc)
                return
            socketio.start_background_task(pixverse_queue_worker)
            pixverse_worker_started = True

    def ensure_template2_worker_started():
        nonlocal template2_worker_started
        with template2_worker_lock:
            if template2_worker_started:
                return
            try:
                import redis  # noqa: F401
            except Exception:
                app.logger.warning("Template2 worker disabled: package 'redis' is not installed")
                return
            try:
                redis_client().ping()
            except Exception as exc:
                app.logger.warning("Template2 worker disabled: Redis unavailable (%s)", exc)
                return
            socketio.start_background_task(template2_queue_worker)
            template2_worker_started = True

    def ensure_template3_worker_started():
        nonlocal template3_worker_started
        with template3_worker_lock:
            if template3_worker_started:
                return
            try:
                import redis  # noqa: F401
            except Exception:
                app.logger.warning("Template3 worker disabled: package 'redis' is not installed")
                return
            try:
                redis_client().ping()
            except Exception as exc:
                app.logger.warning("Template3 worker disabled: Redis unavailable (%s)", exc)
                return
            socketio.start_background_task(template3_queue_worker)
            template3_worker_started = True

    def ensure_template4_worker_started():
        nonlocal template4_worker_started
        with template4_worker_lock:
            if template4_worker_started:
                return
            try:
                import redis  # noqa: F401
            except Exception:
                app.logger.warning("Template4 worker disabled: package 'redis' is not installed")
                return
            try:
                redis_client().ping()
            except Exception as exc:
                app.logger.warning("Template4 worker disabled: Redis unavailable (%s)", exc)
                return
            socketio.start_background_task(template4_queue_worker)
            template4_worker_started = True

    def resume_template4_processing_jobs():
        root = templates4_root_dir()
        if not os.path.isdir(root):
            return
        for entry_name in os.listdir(root):
            template_dir = safe_template4_dir(entry_name)
            if not template_dir:
                continue
            scenes = load_template4_scenes(template_dir)
            changed = False
            for scene in scenes:
                status = str(scene.get("status") or "").lower()
                msg = str(scene.get("message") or "")
                if status not in {"queued", "processing", "failed"}:
                    continue
                scene_id = str(scene.get("id") or "").strip()
                task_id = str(scene.get("kling_task_id") or "").strip()
                if not task_id and (status in {"failed"} and "task_id не сохранен" in msg.lower()):
                    task_id = find_template4_task_id_in_log(entry_name, scene_id)
                    if task_id:
                        scene["kling_task_id"] = task_id
                if not scene_id:
                    continue
                if task_id:
                    scene["status"] = "queued"
                    scene["message"] = f"Восстановлено после перезапуска. Продолжаю по task_id={task_id}"
                    scene["updated_at"] = datetime.now(timezone.utc).isoformat()
                    changed = True
                    try:
                        enqueue_template4_job(entry_name, scene_id)
                    except Exception as exc:
                        scene["status"] = "failed"
                        scene["message"] = f"Не удалось восстановить задачу: {exc}"
                        scene["updated_at"] = datetime.now(timezone.utc).isoformat()
                else:
                    scene["status"] = "failed"
                    scene["message"] = "Задача зависла после перезапуска (task_id не сохранен). Нажмите «Сгенерировать» снова."
                    scene["updated_at"] = datetime.now(timezone.utc).isoformat()
                    changed = True
            if changed:
                save_template4_scenes(template_dir, scenes)

    def ensure_avatar_worker_started():
        nonlocal avatar_worker_started
        with avatar_worker_lock:
            if avatar_worker_started:
                return
            try:
                import redis  # noqa: F401
            except Exception:
                app.logger.warning("Avatar worker disabled: package 'redis' is not installed")
                return
            try:
                redis_client().ping()
            except Exception as exc:
                app.logger.warning("Avatar worker disabled: Redis unavailable (%s)", exc)
                return
            socketio.start_background_task(avatar_queue_worker)
            avatar_worker_started = True

    # -------------------------------------------------------- aivideoapi helpers

    def api_headers():
        return {
            "Authorization": f"Bearer {app.config['AIVIDEOAPI_KEY']}",
            "Content-Type": "application/json",
        }

    def submit_generation(avatar_id: int, photo_url: str, style: dict) -> str | None:
        callback_url = f"{app.config['SERVER']}/api/callback/{avatar_id}"
        payload = {
            "model": app.config["AIVIDEO_IMAGE_MODEL"],
            "input": {
                "prompt": style["prompt_suffix"],
                "image_urls": [photo_url],
                "resolution": "1K",
                "output_format": "jpg",
                "callback_url": callback_url,
            },
        }
        app.logger.info("Submitting avatar=%d style=%s photo=%s callback=%s",
                        avatar_id, style["name"], photo_url, callback_url)
        try:
            resp = requests.post(
                f"{app.config['AIVIDEOAPI_BASE_URL']}/v1/images/generations",
                json=payload,
                headers=api_headers(),
                timeout=30,
            )
            resp.raise_for_status()
            task_id = resp.json().get("data", {}).get("taskId")
            app.logger.info("Submitted avatar=%d task_id=%s", avatar_id, task_id)
            return task_id
        except Exception as exc:
            app.logger.error("submit_generation error avatar=%d: %s", avatar_id, exc)
            return None

    def fetch_avatar_task_result(task_id: str) -> dict | None:
        """Pull current task status from API. Returns dict or None on error."""
        try:
            resp = requests.get(
                f"{app.config['AIVIDEOAPI_BASE_URL']}/v1/tasks/{task_id}",
                headers=api_headers(),
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            app.logger.error("fetch_avatar_task_result error task=%s: %s", task_id, exc)
            return None

    def fetch_video_task_result(task_id: str) -> dict | None:
        try:
            resp = requests.get(
                f"{app.config['AIVIDEOAPI_BASE_URL']}/v1/tasks/{task_id}",
                headers=api_headers(),
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            app.logger.error("fetch_video_task_result error task=%s: %s", task_id, exc)
            return None

    def probe_audio_duration_seconds(input_path: str) -> float:
        try:
            proc = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    input_path,
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            return max(0.0, float((proc.stdout or "0").strip()))
        except Exception:
            return 0.0

    def to_public_asset_url(template_name: str, ref: str) -> str:
        value = (ref or "").strip()
        if not value:
            return ""
        if value.startswith(("http://", "https://")):
            return value
        if value.startswith("/"):
            return f"{app.config['SERVER']}{value}"
        return f"{app.config['SERVER']}/static/templates4/{template_name}/{value}"

    def gemini_headers(content_type_json: bool = True) -> dict:
        api_key = (app.config.get("GEMINI_API_KEY") or "").strip()
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY не задан в .env.")
        headers = {
            "x-goog-api-key": api_key,
            "Accept": "application/json",
        }
        if content_type_json:
            headers["Content-Type"] = "application/json"
        return headers

    def _b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    def kling_bearer_token() -> str:
        direct_api_key = (app.config.get("KLING_API_KEY") or "").strip()
        if direct_api_key:
            return direct_api_key
        access_key = (app.config.get("KLING_ACCESS_KEY") or "").strip()
        secret_key = (app.config.get("KLING_SECRET_KEY") or "").strip()
        if not access_key or not secret_key:
            raise RuntimeError("Не заданы KLING_ACCESS_KEY/KLING_SECRET_KEY (или KLING_API_KEY).")
        now = int(time.time())
        ttl = max(60, min(int(app.config.get("KLING_TOKEN_TTL_SECONDS", 1800)), 1800))
        header = {"alg": "HS256", "typ": "JWT"}
        payload = {
            "iss": access_key,
            "exp": now + ttl,
            "nbf": now - 5,
        }
        header_b64 = _b64url(json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        payload_b64 = _b64url(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        signature = hmac.new(secret_key.encode("utf-8"), signing_input, hashlib.sha256).digest()
        return f"{header_b64}.{payload_b64}.{_b64url(signature)}"

    def kling_headers(content_type_json: bool = True) -> dict:
        token = kling_bearer_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        if content_type_json:
            headers["Content-Type"] = "application/json"
        return headers

    def kling_request(method: str, url: str, *, json_body=None, timeout: int = 30, allow_429_retry: bool = True):
        nonlocal kling_next_request_ts
        with kling_rate_lock:
            now = time.time()
            if now < kling_next_request_ts:
                time.sleep(kling_next_request_ts - now)
            # baseline small spacing between requests
            kling_next_request_ts = time.time() + 1.0
        resp = requests.request(
            method=method.upper(),
            url=url,
            json=json_body,
            headers=kling_headers(content_type_json=(json_body is not None)),
            timeout=timeout,
        )
        if resp.status_code == 429 and allow_429_retry:
            retry_after_raw = (resp.headers.get("Retry-After") or "").strip()
            try:
                retry_after = max(1, int(retry_after_raw))
            except Exception:
                retry_after = 45
            with kling_rate_lock:
                kling_next_request_ts = max(kling_next_request_ts, time.time() + retry_after)
            time.sleep(retry_after)
            resp = requests.request(
                method=method.upper(),
                url=url,
                json=json_body,
                headers=kling_headers(content_type_json=(json_body is not None)),
                timeout=timeout,
            )
        return resp

    def detect_mime_type_by_ext(path: str) -> str:
        ext = os.path.splitext(path)[1].lower()
        if ext in {".jpg", ".jpeg"}:
            return "image/jpeg"
        if ext == ".webp":
            return "image/webp"
        if ext == ".gif":
            return "image/gif"
        return "image/png"

    def image_ref_to_bytes(image_ref: str) -> bytes:
        ref = (image_ref or "").strip()
        if not ref:
            raise RuntimeError("Пустая ссылка на изображение.")
        if ref.startswith(("http://", "https://")):
            resp = requests.get(ref, timeout=60)
            resp.raise_for_status()
            return resp.content
        if ref.startswith("/static/"):
            local_path = os.path.join(app.static_folder, ref[len("/static/"):])
            if not os.path.isfile(local_path):
                raise RuntimeError(f"Локальный файл изображения не найден: {ref}")
            with open(local_path, "rb") as f:
                return f.read()
        if ref.startswith("/"):
            server = (app.config.get("SERVER") or "").rstrip("/")
            if not server:
                raise RuntimeError("SERVER не задан для преобразования относительной ссылки изображения.")
            resp = requests.get(f"{server}{ref}", timeout=60)
            resp.raise_for_status()
            return resp.content
        if os.path.isfile(ref):
            with open(ref, "rb") as f:
                return f.read()
        raise RuntimeError(f"Не удалось прочитать изображение: {ref}")

    def apply_task_result(avatar: CartoonAvatar, data: dict) -> bool:
        """Update avatar from API response dict. Returns True if status changed."""
        # Normalise both top-level and nested shapes
        inner = data.get("data", data)
        status = inner.get("status", "")
        urls = inner.get("output", {}).get("urls", [])

        if status == "completed" and urls:
            avatar.status = "completed"
            source_url = urls[0]
            cached_url = cache_avatar_image(avatar, source_url)
            avatar.image_url = cached_url or source_url
            return True
        if status == "failed":
            avatar.status = "failed"
            return True
        return False  # still processing

    # ---------------------------------------------------------- openai helpers

    _openai_client = None

    def openai_client() -> OpenAI:
        nonlocal _openai_client
        if _openai_client is None:
            _openai_client = OpenAI(api_key=app.config["OPENAI_API_KEY"])
        return _openai_client

    def call_openai(prompt: str) -> str:
        response = openai_client().chat.completions.create(
            model=app.config["OPENAI_STORYBOARD_MODEL"],
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2048,
            timeout=60,
        )
        return response.choices[0].message.content

    def generate_avatar_with_openai(child: Child, avatar: CartoonAvatar, style: dict) -> tuple[bool, str | None]:
        if not app.config.get("OPENAI_API_KEY"):
            return False, "OPENAI_API_KEY не задан в .env."

        photo_path = os.path.join(app.config["UPLOAD_FOLDER"], child.photo_filename)
        if not os.path.isfile(photo_path):
            return False, "Фото ребёнка не найдено на диске."

        prompt = (
            f"{style['prompt_suffix']}\n"
            "Create ONLY one child character avatar based on the child from the input photo. "
            "Ignore all background and extra objects from the original photo. "
            "No objects in hands, hands must be empty and visible. "
            "No toys, books, phones, cups, bags, or accessories in hands. "
            "No other people, no pets, no text, no logos. "
            "Clean plain background, full character figurine style, centered composition, high quality."
        )

        try:
            with open(photo_path, "rb") as image_file:
                try:
                    result = openai_client().images.edit(
                        model=app.config["OPENAI_AVATAR_MODEL"],
                        image=image_file,
                        prompt=prompt,
                        size=app.config["OPENAI_AVATAR_SIZE"],
                    )
                except TypeError:
                    image_file.seek(0)
                    result = openai_client().images.edit(
                        model=app.config["OPENAI_AVATAR_MODEL"],
                        image=image_file,
                        prompt=prompt,
                    )
        except Exception as exc:
            return False, f"Ошибка OpenAI Images API: {exc}"

        data = getattr(result, "data", None) or []
        if not data:
            return False, "OpenAI не вернул данные изображения."
        first = data[0]

        b64_json = getattr(first, "b64_json", None)
        if b64_json is None and isinstance(first, dict):
            b64_json = first.get("b64_json")
        image_url = getattr(first, "url", None)
        if image_url is None and isinstance(first, dict):
            image_url = first.get("url")

        output_dir = avatar_cache_dir()
        output_name = f"avatar_{avatar.id}.png"
        output_path = os.path.join(output_dir, output_name)

        try:
            if b64_json:
                with open(output_path, "wb") as f:
                    f.write(base64.b64decode(b64_json))
            elif image_url:
                downloaded = requests.get(image_url, timeout=60)
                downloaded.raise_for_status()
                with open(output_path, "wb") as f:
                    f.write(downloaded.content)
            else:
                return False, "OpenAI не вернул ни base64, ни URL изображения."
        except Exception as exc:
            return False, f"Не удалось сохранить изображение аватара: {exc}"

        avatar.status = "completed"
        avatar.task_id = None
        avatar.image_url = f"/static/generated/avatar_cache/{output_name}"
        return True, None

    def generated_dir_for_cartoon(cartoon_id: int) -> str:
        folder = os.path.join(app.config["GENERATED_FOLDER"], f"cartoon_{cartoon_id}")
        os.makedirs(folder, exist_ok=True)
        return folder

    def final_video_path_for_cartoon(cartoon_id: int) -> str:
        return os.path.join(generated_dir_for_cartoon(cartoon_id), "final_with_audio.mp4")

    def final_video_url_for_cartoon(cartoon_id: int) -> str | None:
        path = final_video_path_for_cartoon(cartoon_id)
        if not os.path.exists(path):
            return None
        return f"/static/generated/cartoon_{cartoon_id}/final_with_audio.mp4"

    def scene_asset_path(scene: CartoonScene, suffix: str) -> str:
        output_dir = generated_dir_for_cartoon(scene.cartoon_id)
        return os.path.join(output_dir, f"scene_{scene.scene_number:02d}_{suffix}")

    def scene_asset_url(scene: CartoonScene, filename: str) -> str:
        return f"{server_url()}/static/generated/cartoon_{scene.cartoon_id}/{filename}"

    def avatar_cache_dir() -> str:
        path = os.path.join(app.config["GENERATED_FOLDER"], "avatar_cache")
        os.makedirs(path, exist_ok=True)
        return path

    def cache_avatar_image(avatar: CartoonAvatar, source_url: str) -> str | None:
        try:
            parsed = urlparse(source_url)
            ext = os.path.splitext(parsed.path)[1].lower()
            if ext not in {".png", ".jpg", ".jpeg", ".webp"}:
                ext = ".jpg"
            local_name = f"avatar_{avatar.id}{ext}"
            local_path = os.path.join(avatar_cache_dir(), local_name)

            resp = requests.get(source_url, timeout=60)
            resp.raise_for_status()
            with open(local_path, "wb") as f:
                f.write(resp.content)

            return f"/static/generated/avatar_cache/{local_name}"
        except Exception as exc:
            app.logger.warning("Failed to cache avatar image avatar=%s: %s", avatar.id, exc)
            return None

    def eleven_headers(content_type_json: bool = True) -> dict:
        api_key = (app.config.get("ELEVENLABS_API_KEY") or "").strip()
        headers = {
            "xi-api-key": api_key,
            "Accept": "audio/mpeg",
        }
        if content_type_json:
            headers["Content-Type"] = "application/json"
        return headers

    def ffmpeg_available() -> bool:
        try:
            subprocess.run(["ffmpeg", "-version"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            return False

    def download_binary(url: str, output_path: str) -> bool:
        try:
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
            with open(output_path, "wb") as f:
                f.write(resp.content)
            return True
        except Exception as exc:
            app.logger.error("download_binary failed url=%s error=%s", url, exc)
            return False

    def stitch_videos(scene_video_paths: list[str], output_path: str) -> bool:
        if not ffmpeg_available():
            app.logger.error("ffmpeg not available; cannot stitch videos")
            return False
        if not scene_video_paths:
            return False
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt") as concat_file:
            concat_path = concat_file.name
            for p in scene_video_paths:
                escaped = p.replace("'", "'\\''")
                concat_file.write(f"file '{escaped}'\n")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_path, "-c", "copy", output_path],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            return True
        except Exception as exc:
            app.logger.error("stitch_videos failed: %s", exc)
            return False
        finally:
            if os.path.exists(concat_path):
                os.remove(concat_path)

    def eleven_tts(narration_text: str, output_mp3_path: str, voice_id: str | None = None) -> bool:
        effective_voice_id = voice_id or app.config["ELEVENLABS_VOICE_ID"]
        if not app.config["ELEVENLABS_API_KEY"] or not effective_voice_id:
            app.logger.warning("ELEVENLABS_API_KEY or voice_id not set; skip narration")
            return False
        url = f"{app.config['ELEVENLABS_BASE_URL']}/v1/text-to-speech/{effective_voice_id}"
        payload = {
            "text": narration_text,
            "model_id": app.config["ELEVENLABS_VOICE_MODEL"],
            "voice_settings": {
                "stability": 0.45,
                "similarity_boost": 0.8,
                "style": 0.6,
                "use_speaker_boost": True,
            },
        }
        try:
            resp = requests.post(url, headers=eleven_headers(), json=payload, timeout=120)
            resp.raise_for_status()
            with open(output_mp3_path, "wb") as f:
                f.write(resp.content)
            return True
        except Exception as exc:
            app.logger.error("eleven_tts failed: %s", exc)
            return False

    def eleven_tts_bytes(narration_text: str, voice_id: str | None = None) -> tuple[bytes | None, str | None]:
        effective_voice_id = voice_id or app.config["ELEVENLABS_VOICE_ID"]
        if not app.config["ELEVENLABS_API_KEY"] or not effective_voice_id:
            return None, "missing_credentials"
        url = f"{app.config['ELEVENLABS_BASE_URL']}/v1/text-to-speech/{effective_voice_id}"
        payload = {
            "text": narration_text,
            "model_id": app.config["ELEVENLABS_VOICE_MODEL"],
            "voice_settings": {
                "stability": 0.45,
                "similarity_boost": 0.8,
                "style": 0.6,
                "use_speaker_boost": True,
            },
        }
        try:
            resp = requests.post(url, headers=eleven_headers(), json=payload, timeout=120)
            resp.raise_for_status()
            return resp.content, None
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status == 401:
                app.logger.error("eleven_tts_bytes unauthorized (401). Check ELEVENLABS_API_KEY.")
                return None, "unauthorized"
            if status == 404:
                app.logger.error("eleven_tts_bytes voice not found (404) voice_id=%s", effective_voice_id)
                return None, "voice_not_found"
            app.logger.error("eleven_tts_bytes http error status=%s body=%s", status, (exc.response.text if exc.response is not None else ""))
            return None, "http_error"
        except Exception as exc:
            app.logger.error("eleven_tts_bytes failed: %s", exc)
            return None, "unknown_error"

    def concat_audio_files(audio_paths: list[str], output_path: str) -> bool:
        if not ffmpeg_available():
            return False
        if not audio_paths:
            return False
        cmd = ["ffmpeg", "-y"]
        for p in audio_paths:
            cmd.extend(["-i", p])
        concat_inputs = "".join([f"[{idx}:a]" for idx in range(len(audio_paths))])
        filter_complex = f"{concat_inputs}concat=n={len(audio_paths)}:v=0:a=1[outa]"
        cmd.extend([
            "-filter_complex", filter_complex,
            "-map", "[outa]",
            "-c:a", "mp3",
            output_path,
        ])
        try:
            subprocess.run(
                cmd,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            return True
        except Exception as exc:
            app.logger.error("concat_audio_files failed: %s", exc)
            return False

    def parse_dialogue_lines(dialogue: str) -> list[tuple[str, str]]:
        lines = []
        for raw_line in (dialogue or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            speaker = ""
            text = ""
            for sep in [":", "：", "—", "-"]:
                if sep in line:
                    speaker, text = line.split(sep, 1)
                    break
            speaker = speaker.strip()
            text = text.strip().strip("«»\"'")
            if speaker and text:
                lines.append((speaker, text))
        return lines

    def short_narrator_line(text: str, max_words: int = 8) -> str:
        cleaned = " ".join((text or "").replace("\n", " ").split()).strip()
        if not cleaned:
            return ""
        words = cleaned.split(" ")
        if len(words) <= max_words:
            return cleaned
        return " ".join(words[:max_words]).rstrip(",;:.") + "..."

    def resolve_voice_id_for_name(cartoon: Cartoon, speaker_name: str) -> str | None:
        key = (speaker_name or "").strip().lower()
        for p in cartoon.participants:
            if p.child.name.strip().lower() == key and p.child.voice_id:
                return p.child.voice_id
        for cl in cartoon.character_links:
            if cl.character.name.strip().lower() == key and cl.character.voice_id:
                return cl.character.voice_id
        return app.config["NARRATOR_DEFAULT_VOICE_ID"] or app.config["ELEVENLABS_VOICE_ID"] or None

    def build_scene_image_prompt(cartoon: Cartoon, scene: CartoonScene) -> str:
        return (
            "Create one cinematic keyframe image for the START of this scene. "
            "Children animation, bright and coherent with prior scenes.\n"
            f"Cartoon idea: {cartoon.story_prompt}\n"
            f"Scene title: {scene.title}\n"
            f"Scene action: {scene.description}\n"
            f"Scene visual details: {scene.visual_description}\n"
            f"Global style lock: {build_style_lock(cartoon)}\n"
            f"Character lock: {build_character_identity_lock(cartoon)}\n"
            "Frame: 16:9, clean composition, readable character poses."
        )

    def generate_scene_start_image(cartoon: Cartoon, scene: CartoonScene) -> tuple[str | None, str | None]:
        if not app.config.get("OPENAI_API_KEY"):
            return None, None
        try:
            image = openai_client().images.generate(
                model=app.config["OPENAI_IMAGE_MODEL"],
                prompt=build_scene_image_prompt(cartoon, scene),
                size="1536x1024",
                quality="high",
                output_format="jpeg",
            )
            if not image.data:
                return None, None
            path = scene_asset_path(scene, "start.jpg")
            item = image.data[0]
            b64 = getattr(item, "b64_json", None)
            if b64:
                image_bytes = base64.b64decode(b64)
                with open(path, "wb") as f:
                    f.write(image_bytes)
            else:
                url = getattr(item, "url", None)
                if not url or not download_binary(url, path):
                    return None, None
            return path, scene_asset_url(scene, f"scene_{scene.scene_number:02d}_start.jpg")
        except Exception as exc:
            app.logger.error("generate_scene_start_image failed scene=%d: %s", scene.id, exc)
            return None, None

    def generate_scene_dialogue_audio(cartoon: Cartoon, scene: CartoonScene, output_mp3_path: str) -> bool:
        parsed_dialogue = parse_dialogue_lines(scene.dialogue or "")
        items = []
        # Prefer character speech. Narrator should be short and optional.
        if parsed_dialogue:
            items.extend(parsed_dialogue)
            if len(parsed_dialogue) <= 1 and scene.description:
                narrator_line = short_narrator_line(scene.description, max_words=6)
                if narrator_line:
                    items.insert(0, ("narrator", narrator_line))
        elif scene.description:
            narrator_line = short_narrator_line(scene.description, max_words=8)
            if narrator_line:
                items.append(("narrator", narrator_line))
        if not items:
            return False

        segments = []
        for idx, (speaker, text) in enumerate(items, start=1):
            seg_path = scene_asset_path(scene, f"voice_seg_{idx:02d}.mp3")
            if speaker == "narrator":
                voice_id = app.config["NARRATOR_DEFAULT_VOICE_ID"] or app.config["ELEVENLABS_VOICE_ID"]
            else:
                voice_id = resolve_voice_id_for_name(cartoon, speaker)
            if eleven_tts(text, seg_path, voice_id=voice_id):
                segments.append(seg_path)
        if not segments:
            return False
        return concat_audio_files(segments, output_mp3_path)

    def prepare_scene_assets(cartoon: Cartoon, scene: CartoonScene) -> dict:
        assets = {"image_path": None, "image_url": None, "voice_path": None, "sfx_path": None, "mix_path": None}

        assets["image_path"], assets["image_url"] = generate_scene_start_image(cartoon, scene)

        voice_path = scene_asset_path(scene, "voice.mp3")
        if generate_scene_dialogue_audio(cartoon, scene, voice_path):
            assets["voice_path"] = voice_path

        sfx_path = scene_asset_path(scene, "sfx.mp3")
        sfx_prompt = scene.sound_effects or scene.description or "Soft magical cartoon ambience"
        if eleven_sfx(sfx_prompt, sfx_path):
            assets["sfx_path"] = sfx_path

        mix_path = scene_asset_path(scene, "mix.mp3")
        if mix_audio_files([assets["voice_path"], assets["sfx_path"]], mix_path):
            assets["mix_path"] = mix_path
        elif assets["voice_path"] and os.path.exists(assets["voice_path"]):
            assets["mix_path"] = assets["voice_path"]
        elif assets["sfx_path"] and os.path.exists(assets["sfx_path"]):
            assets["mix_path"] = assets["sfx_path"]

        return assets

    def generate_dialogue_narration(cartoon: Cartoon, output_mp3_path: str) -> bool:
        scene_items = []
        for scene in sorted(cartoon.scenes, key=lambda s: s.scene_number):
            parsed = parse_dialogue_lines(scene.dialogue or "")
            if parsed:
                for speaker, text in parsed:
                    scene_items.append((speaker, text))
            elif scene.description:
                scene_items.append(("narrator", scene.description.strip()))
        if not scene_items:
            return False

        output_dir = generated_dir_for_cartoon(cartoon.id)
        segment_paths = []
        for idx, (speaker, text) in enumerate(scene_items, start=1):
            segment_path = os.path.join(output_dir, f"tts_segment_{idx:03d}.mp3")
            voice_id = resolve_voice_id_for_name(cartoon, speaker)
            if eleven_tts(text, segment_path, voice_id=voice_id):
                segment_paths.append(segment_path)

        if not segment_paths:
            return False
        return concat_audio_files(segment_paths, output_mp3_path)

    def eleven_music(music_prompt: str, output_mp3_path: str) -> bool:
        if not app.config["ELEVENLABS_API_KEY"]:
            return False
        # Endpoint compatibility may vary between accounts. Keep graceful fallback.
        url = f"{app.config['ELEVENLABS_BASE_URL']}/v1/music/generate"
        payload = {
            "model_id": app.config["ELEVENLABS_MUSIC_MODEL"],
            "prompt": music_prompt,
            "format": "mp3",
            "duration_seconds": 30,
        }
        try:
            resp = requests.post(url, headers=eleven_headers(), json=payload, timeout=180)
            resp.raise_for_status()
            with open(output_mp3_path, "wb") as f:
                f.write(resp.content)
            return True
        except Exception as exc:
            app.logger.warning("eleven_music failed: %s", exc)
            return False

    def normalize_sfx_prompt(raw_prompt: str, max_chars: int = 220) -> str:
        prompt = " ".join((raw_prompt or "").replace("\n", " ").split()).strip()
        if not prompt:
            prompt = "soft magical ambience, gentle forest wind, playful leaves rustling, birds"
        prompt = re.sub(r"[\"'`]+", "", prompt)
        if len(prompt) > max_chars:
            trimmed = prompt[:max_chars]
            if " " in trimmed:
                trimmed = trimmed.rsplit(" ", 1)[0]
            prompt = f"{trimmed}."
        # Force non-verbal SFX to avoid accidental spoken output.
        return (
            "Sound effects only. Non-verbal ambience, no speech, no narration, "
            f"no spoken words, no lyrics. {prompt}"
        )

    def fallback_sfx_ffmpeg(output_mp3_path: str, duration_seconds: float = 6.0) -> bool:
        if not ffmpeg_available():
            return False
        try:
            duration = max(1.0, min(float(duration_seconds), 12.0))
        except Exception:
            duration = 6.0
        try:
            cmd = [
                "ffmpeg", "-y",
                "-f", "lavfi",
                "-i", f"anoisesrc=color=pink:amplitude=0.04:duration={duration}",
                "-af", "highpass=f=180,lowpass=f=5000,volume=0.6",
                "-c:a", "mp3",
                output_mp3_path,
            ]
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            app.logger.warning("eleven_sfx fallback: generated local fx via ffmpeg path=%s", output_mp3_path)
            return True
        except Exception as exc:
            app.logger.error("fallback_sfx_ffmpeg failed: %s", exc)
            return False

    def eleven_sfx(sfx_prompt: str, output_mp3_path: str) -> bool:
        if not app.config["ELEVENLABS_API_KEY"]:
            return False
        url = f"{app.config['ELEVENLABS_BASE_URL']}/v1/sound-generation"
        duration = max(0.5, min(float(app.config.get("ELEVENLABS_SFX_DURATION_SECONDS", 6)), 22.0))
        normalized_prompt = normalize_sfx_prompt(
            sfx_prompt,
            max_chars=int(app.config.get("ELEVENLABS_SFX_PROMPT_MAX_CHARS", 220)),
        )
        model_candidates = []
        configured_model = (app.config.get("ELEVENLABS_SFX_MODEL") or "").strip()
        if configured_model:
            model_candidates.append(configured_model)
        if "eleven_text_to_sound_v2" not in model_candidates:
            model_candidates.append("eleven_text_to_sound_v2")

        payload_variants = []
        for model_id in model_candidates:
            payload_variants.append(
                {
                    "model_id": model_id,
                    "text": normalized_prompt,
                    "duration_seconds": duration,
                    "prompt_influence": 0.45,
                    "output_format": "mp3_44100_128",
                }
            )
            payload_variants.append(
                {
                    "model_id": model_id,
                    "text": normalized_prompt,
                    "duration_seconds": duration,
                    "output_format": "mp3_44100_128",
                }
            )
        payload_variants.extend([
            {
                "text": normalized_prompt,
                "duration_seconds": duration,
            },
            {
                "text": normalized_prompt,
                "output_format": "mp3_44100_128",
            },
            {
                "text": normalized_prompt,
            },
        ])
        for idx, payload in enumerate(payload_variants, start=1):
            try:
                resp = requests.post(url, headers=eleven_headers(), json=payload, timeout=20)
                resp.raise_for_status()
                with open(output_mp3_path, "wb") as f:
                    f.write(resp.content)
                return True
            except requests.HTTPError as exc:
                body = exc.response.text if exc.response is not None else ""
                app.logger.warning("eleven_sfx attempt=%d failed status=%s body=%s", idx, exc.response.status_code if exc.response is not None else None, body)
                # Do not keep waiting on clearly invalid auth/access errors.
                if exc.response is not None and exc.response.status_code in (401, 403, 404):
                    break
            except Exception as exc:
                app.logger.warning("eleven_sfx attempt=%d failed: %s", idx, exc)
        # Keep pipeline alive even if ElevenLabs SFX endpoint is temporarily unavailable.
        return fallback_sfx_ffmpeg(output_mp3_path, duration_seconds=duration)

    def mix_audio_files(audio_paths: list[str], output_path: str) -> bool:
        if not ffmpeg_available():
            return False
        existing = [p for p in audio_paths if p and os.path.exists(p)]
        if not existing:
            return False
        app.logger.info("mix_audio_files inputs=%s output=%s", existing, output_path)
        if len(existing) == 1:
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-i", existing[0], "-c:a", "mp3", output_path],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                return True
            except Exception as exc:
                app.logger.error("mix_audio_files copy failed: %s", exc)
                return False

        # Prefer explicit voice + sfx balancing for 2-track scene mix.
        cmd = ["ffmpeg", "-y"]
        for p in existing:
            cmd.extend(["-i", p])
        if len(existing) == 2:
            filter_complex = (
                "[0:a]volume=1.0[a0];"
                "[1:a]volume=2.4[a1];"
                "[a0][a1]amix=inputs=2:duration=longest:weights='1 1.6':normalize=0,"
                "alimiter=limit=0.95[aout]"
            )
        else:
            mix_inputs = "".join([f"[{idx}:a]" for idx in range(len(existing))])
            filter_complex = f"{mix_inputs}amix=inputs={len(existing)}:duration=longest:normalize=0,alimiter=limit=0.95[aout]"
        cmd.extend(["-filter_complex", filter_complex, "-map", "[aout]", "-c:a", "mp3", output_path])
        try:
            subprocess.run(
                cmd,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            return True
        except Exception as exc:
            app.logger.warning("mix_audio_files primary mix failed, trying fallback: %s", exc)
            # Fallback to simpler amix syntax for maximum compatibility.
            fallback_cmd = ["ffmpeg", "-y"]
            for p in existing:
                fallback_cmd.extend(["-i", p])
            mix_inputs = "".join([f"[{idx}:a]" for idx in range(len(existing))])
            fallback_filter = f"{mix_inputs}amix=inputs={len(existing)}:duration=longest[aout]"
            fallback_cmd.extend(["-filter_complex", fallback_filter, "-map", "[aout]", "-c:a", "mp3", output_path])
            try:
                subprocess.run(
                    fallback_cmd,
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                return True
            except Exception as fallback_exc:
                app.logger.error("mix_audio_files failed: %s", fallback_exc)
                return False

    def add_audio_to_video(video_path: str, audio_path: str, output_path: str) -> bool:
        if not ffmpeg_available():
            return False
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-i", video_path,
                    "-i", audio_path,
                    "-map", "0:v:0",
                    "-map", "1:a:0",
                    "-c:v", "copy",
                    "-c:a", "aac",
                    "-shortest",
                    output_path,
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            return True
        except Exception as exc:
            app.logger.error("add_audio_to_video failed: %s", exc)
            return False

    def add_audio_tracks(base_video_path: str, narration_path: str | None, music_path: str | None, sfx_path: str | None, output_path: str) -> bool:
        if not ffmpeg_available():
            return False

        inputs = [base_video_path]
        audio_inputs = []
        for p in [narration_path, music_path, sfx_path]:
            if p and os.path.exists(p):
                inputs.append(p)
                audio_inputs.append(len(inputs) - 1)

        if not audio_inputs:
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-i", base_video_path, "-c:v", "copy", "-an", output_path],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                return True
            except Exception as exc:
                app.logger.error("add_audio_tracks passthrough failed: %s", exc)
                return False

        cmd = ["ffmpeg", "-y"]
        for p in inputs:
            cmd.extend(["-i", p])

        mix_inputs = "".join([f"[{idx}:a]" for idx in audio_inputs])
        filter_complex = f"{mix_inputs}amix=inputs={len(audio_inputs)}:duration=longest[aout]"
        cmd.extend([
            "-filter_complex", filter_complex,
            "-map", "0:v:0",
            "-map", "[aout]",
            "-c:v", "copy",
            "-c:a", "aac",
            "-shortest",
            output_path,
        ])
        try:
            subprocess.run(
                cmd,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            return True
        except Exception as exc:
            app.logger.error("add_audio_tracks failed: %s", exc)
            return False

    def child_reference_url(child: Child) -> str:
        """Best visual reference URL for child character identity."""
        if child.selected_avatar and child.selected_avatar.image_url:
            return child.selected_avatar.image_url
        return f"{app.config['SERVER']}/static/uploads/{child.photo_filename}"

    def character_reference_url(character: Character) -> str | None:
        if not character.image_filename:
            return None
        return f"{app.config['SERVER']}/static/uploads/{character.image_filename}"

    def build_style_lock(cartoon: Cartoon) -> str:
        selected_styles = []
        for p in cartoon.participants:
            if p.child.selected_avatar and p.child.selected_avatar.style_name:
                selected_styles.append(p.child.selected_avatar.style_name)
        style_hint = selected_styles[0] if selected_styles else "storybook"
        return (
            "Unified visual style lock (must remain identical in every scene): "
            f"high-quality children's animated movie look inspired by {style_hint}, "
            "same rendering engine, same color script, same line quality, same character proportions, "
            "same wardrobe, same facial design, same world art direction, 16:9 composition. "
            "Warm fairy-tale lighting, clean cinematic framing, vivid but harmonious palette."
        )

    def build_character_identity_lock(cartoon: Cartoon) -> str:
        lines = []
        for p in cartoon.participants:
            child = p.child
            ref_url = child_reference_url(child)
            avatar_style = child.selected_avatar.style_name if child.selected_avatar else "cartoonized from photo"
            lines.append(
                f"- {child.name}: keep the same face, hairstyle, body proportions and outfit in all scenes; "
                f"visual reference URL: {ref_url}; avatar style: {avatar_style}."
            )

        for cl in cartoon.character_links:
            character = cl.character
            desc = character.description or "no additional description"
            ref_url = character_reference_url(character)
            if ref_url:
                lines.append(
                    f"- {character.name}: {desc}; keep identical design in all scenes; "
                    f"visual reference URL: {ref_url}."
                )
            else:
                lines.append(f"- {character.name}: {desc}; keep identical design in all scenes.")

        return "\n".join(lines) if lines else "- Use a consistent cast with stable appearance."

    def build_video_prompt_lock(cartoon: Cartoon) -> str:
        return (
            f"{build_style_lock(cartoon)}\n"
            "Character identity lock (mandatory):\n"
            f"{build_character_identity_lock(cartoon)}\n"
            "Do not redesign characters between scenes. Keep continuity of costumes, faces and proportions."
        )

    def build_reference_image_urls(cartoon: Cartoon) -> list[str]:
        urls = []
        for p in cartoon.participants:
            urls.append(child_reference_url(p.child))
        for cl in cartoon.character_links:
            ref_url = character_reference_url(cl.character)
            if ref_url:
                urls.append(ref_url)
        # preserve order + dedupe
        unique_urls = []
        seen = set()
        for u in urls:
            if not u or u in seen:
                continue
            unique_urls.append(u)
            seen.add(u)
        # keep payload compact
        return unique_urls[:6]

    def generate_storyboard(cartoon: Cartoon) -> None:
        children_text = "\n".join(
            f"- {p.child.name}" for p in cartoon.participants
        ) or "нет"
        characters_text = "\n".join(
            f"- {cl.character.name}" + (f": {cl.character.description}" if cl.character.description else "")
            for cl in cartoon.character_links
        ) or "нет"

        # Build character appearance hints for the video prompt
        char_hints = []
        for p in cartoon.participants:
            char_hints.append(f"{p.child.name} (real child rendered as cartoon character)")
        for cl in cartoon.character_links:
            desc = f": {cl.character.description}" if cl.character.description else ""
            char_hints.append(f"{cl.character.name}{desc}")
        char_hints_text = "; ".join(char_hints) if char_hints else "cartoon characters"
        style_lock = build_style_lock(cartoon)
        identity_lock = build_character_identity_lock(cartoon)

        prompt = (
            "You are a children's cartoon scriptwriter and AI video prompt engineer.\n"
            "Create a detailed storyboard for a cartoon lasting 30 seconds total.\n\n"
            f"Main characters (real children rendered as cartoon):\n{children_text}\n\n"
            f"Supporting characters:\n{characters_text}\n\n"
            f"Story idea:\n{cartoon.story_prompt}\n\n"
            f"Global style lock:\n{style_lock}\n\n"
            f"Character identity lock (mandatory for every scene):\n{identity_lock}\n\n"
            "Return ONLY valid JSON with no markdown fences, no extra text:\n"
            "{\n"
            '  "title": "Short cartoon title (max 6 words, in Russian)",\n'
            '  "scenes": [\n'
            "    {\n"
            '      "scene_number": 1,\n'
            '      "title": "Scene title in Russian",\n'
            '      "description": "Very short narrator summary in Russian (1 short sentence, 6–10 words max).",\n'
            '      "visual_description": "Detailed visual: background, colors, lighting, character poses (Russian).",\n'
            '      "dialogue": "Character lines only (Russian). Format each line: Name: «text». 3–5 short lines per scene, at least 2 different speakers when possible.",\n'
            '      "weather": "Weather and time of day (Russian).",\n'
            '      "music": "Music description: genre, instruments, tempo, mood (Russian).",\n'
            '      "sound_effects": "Comma-separated sound effects (Russian).",\n'
            '      "facial_expressions": "Per-character facial expression and emotion (Russian).",\n'
            '      "duration_seconds": 5,\n'
            '      "video_prompt": "ENGLISH ONLY. AI video generation prompt for this scene. Must start with exact style lock sentence and include exact character names from this project with stable appearance details. Format: [Fixed style lock sentence]. [Characters and exact actions]. [Background and setting]. [Lighting and colors]. [Camera angle/movement]."\n'
            "    }\n"
            "  ]\n"
            "}\n\n"
            "Requirements:\n"
            "- Exactly 6 scenes, total duration exactly 30 seconds\n"
            "- Each scene duration must be exactly 5 seconds\n"
            "- Children's story for ages 4–8: bright, positive, with beginning, climax and happy ending\n"
            "- Prefer dialogue-driven storytelling: many short character lines, minimal narrator text\n"
            "- Narrator text must be very short (6-10 words max per scene)\n"
            "- Dialogue must contain 3-5 lines per scene (short lines)\n"
            f"- Characters for video_prompt: {char_hints_text}\n"
            "- video_prompt must be in English, 4–6 sentences, rich visual detail, no dialogue text\n"
            "- All scene video_prompt strings must share the same visual style and same character identity\n"
            "- It is forbidden to change character look from one scene to another\n"
            "- All other fields in Russian\n"
            "- Response: JSON only, no extra text"
        )

        app.logger.info("Generating storyboard for cartoon=%d", cartoon.id)
        response_text = call_openai(prompt)

        # strip markdown fences if present
        text = response_text.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            start = 1
            end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
            text = "\n".join(lines[start:end])

        data = json.loads(text)
        cartoon.title = data.get("title", "Мультик")
        for scene_data in data.get("scenes", []):
            scene_prompt = (scene_data.get("video_prompt", "") or "").strip()
            if scene_prompt:
                scene_prompt = (
                    f"{build_video_prompt_lock(cartoon)}\n"
                    f"Scene-specific direction:\n{scene_prompt}\n"
                    "Mandatory continuity: same art style and same character appearance as previous/next scenes."
                )
            db.session.add(CartoonScene(
                cartoon_id=cartoon.id,
                scene_number=scene_data.get("scene_number", 0),
                title=scene_data.get("title", ""),
                description=scene_data.get("description", ""),
                visual_description=scene_data.get("visual_description", ""),
                dialogue=scene_data.get("dialogue", ""),
                weather=scene_data.get("weather", ""),
                music=scene_data.get("music", ""),
                sound_effects=scene_data.get("sound_effects", ""),
                facial_expressions=scene_data.get("facial_expressions", ""),
                duration_seconds=scene_data.get("duration_seconds", 8),
                video_prompt=scene_prompt,
            ))
        cartoon.status = "ready"
        app.logger.info("Storyboard generated for cartoon=%d title=%s", cartoon.id, cartoon.title)

    def submit_video_scene(
        scene: CartoonScene,
        previous_scene_video_url: str | None = None,
        start_image_url: str | None = None,
    ) -> str | None:
        """Submit one scene to AIVIDEOAPI video endpoint. Returns task_id or None."""
        callback_url = f"{app.config['SERVER']}/api/video-callback/{scene.id}"
        duration = max(1, min(scene.duration_seconds or 8, 10))
        payload = {
            "model": app.config["AIVIDEO_VIDEO_MODEL"],
            "callback_url": callback_url,
            "input": {
                "prompt": scene.video_prompt,
                "duration": duration,
                "aspect_ratio": "16:9",
                "resolution": "480p",
            },
        }

        # Try attaching references for better consistency.
        reference_urls = build_reference_image_urls(scene.cartoon)
        if start_image_url:
            reference_urls = [start_image_url] + reference_urls
        if reference_urls:
            payload["input"]["image_urls"] = reference_urls
        if previous_scene_video_url:
            payload["input"]["previous_scene_url"] = previous_scene_video_url

        app.logger.info("Submitting video scene=%d duration=%d prompt=%.80s",
                        scene.id, duration, scene.video_prompt)
        payload_candidates = [payload]
        if reference_urls:
            fallback_no_refs = json.loads(json.dumps(payload))
            fallback_no_refs["input"].pop("image_urls", None)
            payload_candidates.append(fallback_no_refs)

        for idx, candidate in enumerate(payload_candidates, start=1):
            try:
                resp = requests.post(
                    f"{app.config['AIVIDEOAPI_BASE_URL']}/v1/videos/generations",
                    json=candidate,
                    headers=api_headers(),
                    timeout=30,
                )
                resp.raise_for_status()
                task_id = resp.json().get("data", {}).get("taskId")
                app.logger.info("Video submitted scene=%d task_id=%s attempt=%d", scene.id, task_id, idx)
                if task_id:
                    return task_id
            except Exception as exc:
                app.logger.warning("submit_video_scene attempt=%d failed scene=%d: %s", idx, scene.id, exc)

        app.logger.error("submit_video_scene error scene=%d: all payload attempts failed", scene.id)
        return None

    def get_pipeline_status(cartoon: Cartoon) -> dict:
        scenes = list(cartoon.scenes)
        has_scenes = len(scenes) > 0
        images_ready = has_scenes and all(os.path.exists(scene_asset_path(s, "start.jpg")) for s in scenes)
        audio_ready = has_scenes and all(os.path.exists(scene_asset_path(s, "mix.mp3")) for s in scenes)
        video_ready = has_scenes and all((s.video_status == "completed" and s.video_url) for s in scenes)
        final_ready = final_video_url_for_cartoon(cartoon.id) is not None
        return {
            "storyboard": has_scenes,
            "images": images_ready,
            "audio": audio_ready,
            "video": video_ready,
            "assemble": final_ready,
        }

    def build_pipeline_snapshot(cartoon_id: int) -> dict:
        cartoon = Cartoon.query.get(cartoon_id)
        if not cartoon:
            return {"pipeline_status": {}, "final_video_url": None, "scenes": []}
        scenes_payload = []
        for scene in sorted(cartoon.scenes, key=lambda s: s.scene_number):
            image_path = scene_asset_path(scene, "start.jpg")
            voice_path = scene_asset_path(scene, "voice.mp3")
            sfx_path = scene_asset_path(scene, "sfx.mp3")
            mix_path = scene_asset_path(scene, "mix.mp3")
            scenes_payload.append({
                "id": scene.id,
                "scene_number": scene.scene_number,
                "video_status": scene.video_status,
                "video_url": scene.video_url,
                "start_image_url": scene_asset_url(scene, f"scene_{scene.scene_number:02d}_start.jpg") if os.path.exists(image_path) else None,
                "voice_audio_url": scene_asset_url(scene, f"scene_{scene.scene_number:02d}_voice.mp3") if os.path.exists(voice_path) else None,
                "sfx_audio_url": scene_asset_url(scene, f"scene_{scene.scene_number:02d}_sfx.mp3") if os.path.exists(sfx_path) else None,
                "mix_audio_url": scene_asset_url(scene, f"scene_{scene.scene_number:02d}_mix.mp3") if os.path.exists(mix_path) else None,
            })
        return {
            "pipeline_status": get_pipeline_status(cartoon),
            "final_video_url": final_video_url_for_cartoon(cartoon_id),
            "scenes": scenes_payload,
        }

    def get_pipeline_runtime(cartoon_id: int) -> dict:
        with pipeline_runtime_lock:
            state = pipeline_runtime_state.get(cartoon_id)
            if not state:
                return {"running": False, "step": None, "message": ""}
            return {
                "running": bool(state.get("running")),
                "step": state.get("step"),
                "message": state.get("message", ""),
                "status": state.get("status", "idle"),
            }

    def set_pipeline_runtime(cartoon_id: int, *, running: bool, step: str | None, message: str, status: str):
        with pipeline_runtime_lock:
            pipeline_runtime_state[cartoon_id] = {
                "running": running,
                "step": step,
                "message": message,
                "status": status,
            }
        socketio.emit(
            "pipeline_status",
            {
                "cartoon_id": cartoon_id,
                "running": running,
                "step": step,
                "message": message,
                "status": status,
                **build_pipeline_snapshot(cartoon_id),
            },
            room=f"cartoon_{cartoon_id}",
        )

    def start_pipeline_step_async(cartoon_id: int, step: str, runner) -> tuple[bool, str]:
        current = get_pipeline_runtime(cartoon_id)
        if current.get("running"):
            active = current.get("step") or "unknown"
            return False, f"Уже выполняется шаг: {active}. Дождитесь завершения."

        set_pipeline_runtime(
            cartoon_id,
            running=True,
            step=step,
            message=f"Запущен шаг: {step}",
            status="running",
        )

        def _task():
            try:
                with app.app_context():
                    cartoon = Cartoon.query.get(cartoon_id)
                    if not cartoon:
                        set_pipeline_runtime(
                            cartoon_id,
                            running=False,
                            step=step,
                            message="Мультик не найден.",
                            status="error",
                        )
                        return
                    ok, msg = runner(cartoon)
                    set_pipeline_runtime(
                        cartoon_id,
                        running=False,
                        step=step,
                        message=msg,
                        status="success" if ok else "error",
                    )
            except Exception as exc:
                app.logger.error("Pipeline async step failed cartoon=%d step=%s error=%s", cartoon_id, step, exc)
                set_pipeline_runtime(
                    cartoon_id,
                    running=False,
                    step=step,
                    message=f"Шаг {step} завершился с ошибкой.",
                    status="error",
                )

        socketio.start_background_task(_task)
        return True, f"Шаг «{step}» запущен в фоне."

    def run_step_storyboard(cartoon: Cartoon) -> tuple[bool, str]:
        if not app.config.get("OPENAI_API_KEY"):
            return False, "OPENAI_API_KEY не задан в .env."
        for scene in list(cartoon.scenes):
            db.session.delete(scene)
        cartoon.status = "generating"
        db.session.commit()
        try:
            generate_storyboard(cartoon)
            db.session.commit()
            return True, "Шаг 1/5: история сгенерирована."
        except Exception as exc:
            app.logger.error("run_step_storyboard failed cartoon=%d: %s", cartoon.id, exc)
            cartoon.status = "failed"
            db.session.commit()
            return False, "Ошибка генерации истории."

    def run_step_images(cartoon: Cartoon) -> tuple[bool, str]:
        if not cartoon.scenes:
            return False, "Сначала выполните шаг «История»."
        done = 0
        total = len(cartoon.scenes)
        for idx, scene in enumerate(cartoon.scenes, start=1):
            set_pipeline_runtime(
                cartoon.id,
                running=True,
                step="images",
                message=f"Генерируем кадры: {idx}/{total}",
                status="running",
            )
            path, _ = generate_scene_start_image(cartoon, scene)
            if path:
                done += 1
        if done == 0:
            return False, "Не удалось сгенерировать стартовые кадры."
        return True, f"Шаг 2/5: стартовые кадры готовы для {done}/{len(cartoon.scenes)} сцен."

    def run_step_audio(cartoon: Cartoon) -> tuple[bool, str]:
        if not cartoon.scenes:
            return False, "Сначала выполните шаг «История»."
        done = 0
        total = len(cartoon.scenes)
        for idx, scene in enumerate(cartoon.scenes, start=1):
            set_pipeline_runtime(
                cartoon.id,
                running=True,
                step="audio",
                message=f"Генерируем аудио: {idx}/{total}",
                status="running",
            )
            assets = prepare_scene_assets(cartoon, scene)
            if assets.get("mix_path") and os.path.exists(assets["mix_path"]):
                done += 1
        if done == 0:
            return False, "Не удалось подготовить аудио сцен."
        return True, f"Шаг 3/5: аудио готово для {done}/{len(cartoon.scenes)} сцен."

    def run_step_video(cartoon: Cartoon) -> tuple[bool, str]:
        if not app.config.get("AIVIDEOAPI_KEY"):
            return False, "AIVIDEOAPI_KEY не задан в .env."
        scenes_with_prompt = [s for s in cartoon.scenes if s.video_prompt]
        if not scenes_with_prompt:
            return False, "Сначала выполните шаг «История»."

        existing_scene_video_urls = {s.scene_number: s.video_url for s in cartoon.scenes if s.video_url}
        for scene in cartoon.scenes:
            scene.video_status = "pending" if scene.video_prompt else None
            scene.video_task_id = None
            scene.video_url = None
        db.session.commit()

        submitted = 0
        total = len(scenes_with_prompt)
        for idx, scene in enumerate(scenes_with_prompt, start=1):
            set_pipeline_runtime(
                cartoon.id,
                running=True,
                step="video",
                message=f"Запуск видео-сцен: {idx}/{total}",
                status="running",
            )
            start_image = scene_asset_path(scene, "start.jpg")
            start_image_url = scene_asset_url(scene, f"scene_{scene.scene_number:02d}_start.jpg") if os.path.exists(start_image) else None
            task_id = submit_video_scene(
                scene,
                previous_scene_video_url=existing_scene_video_urls.get(scene.scene_number - 1),
                start_image_url=start_image_url,
            )
            if task_id:
                scene.video_task_id = task_id
                submitted += 1
            else:
                scene.video_status = "failed"
        db.session.commit()
        if submitted == 0:
            return False, "Не удалось запустить генерацию видео."
        return True, f"Шаг 4/5: видео запущено для {submitted}/{len(scenes_with_prompt)} сцен."

    def run_step_assemble(cartoon: Cartoon) -> tuple[bool, str]:
        ready_scenes = [s for s in cartoon.scenes if s.video_status == "completed" and s.video_url]
        if not ready_scenes:
            return False, "Нет готовых видео-сцен. Сначала выполните шаг «Видео»."
        if not ffmpeg_available():
            return False, "ffmpeg не найден в системе."

        output_dir = generated_dir_for_cartoon(cartoon.id)
        local_scene_paths = []
        for scene in sorted(ready_scenes, key=lambda s: s.scene_number):
            local_path = os.path.join(output_dir, f"scene_{scene.scene_number:02d}.mp4")
            if not download_binary(scene.video_url, local_path):
                return False, f"Не удалось скачать сцену {scene.scene_number}."
            mixed_audio = scene_asset_path(scene, "mix.mp3")
            dubbed_path = os.path.join(output_dir, f"scene_{scene.scene_number:02d}_dubbed.mp4")
            if os.path.exists(mixed_audio) and add_audio_to_video(local_path, mixed_audio, dubbed_path):
                local_scene_paths.append(dubbed_path)
            else:
                local_scene_paths.append(local_path)

        stitched_path = os.path.join(output_dir, "stitched.mp4")
        if not stitch_videos(local_scene_paths, stitched_path):
            return False, "Ошибка Stitch: не удалось склеить сцены."

        final_path = final_video_path_for_cartoon(cartoon.id)
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", stitched_path, "-c", "copy", final_path],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except Exception as exc:
            app.logger.error("final export failed cartoon=%d: %s", cartoon.id, exc)
            return False, "Ошибка финального экспорта."
        return True, "Шаг 5/5: финальный ролик собран."

    # ------------------------------------------------------------------ routes

    @app.route("/")
    def index():
        children = Child.query.order_by(Child.created_at.desc()).all()
        return render_template("index.html", children=children)

    @app.route("/children/add", methods=["POST"])
    def add_child():
        name = request.form.get("name", "").strip()
        if not name:
            flash("Введите имя ребёнка.", "danger")
            return redirect(url_for("index"))
        voice_id = request.form.get("voice_id", "").strip() or None

        photo = request.files.get("photo")
        if not photo or photo.filename == "":
            flash("Выберите фотографию.", "danger")
            return redirect(url_for("index"))

        if not allowed_file(photo.filename):
            flash("Допустимые форматы: JPG, PNG, WEBP.", "danger")
            return redirect(url_for("index"))

        ext = photo.filename.rsplit(".", 1)[1].lower()
        filename = f"{uuid.uuid4().hex}.{ext}"
        photo.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))

        child = Child(name=name, photo_filename=filename, voice_id=voice_id)
        db.session.add(child)
        db.session.commit()

        flash(f"Ребёнок «{name}» добавлен!", "success")
        return redirect(url_for("child_detail", child_id=child.id))

    @app.route("/children/<int:child_id>")
    def child_detail(child_id):
        child = Child.query.get_or_404(child_id)
        return render_template("child.html", child=child)

    @app.route("/children/<int:child_id>/voice", methods=["POST"])
    def update_child_voice(child_id):
        child = Child.query.get_or_404(child_id)
        child.voice_id = request.form.get("voice_id", "").strip() or None
        db.session.commit()
        flash("Голос ребёнка обновлён.", "success")
        return redirect(url_for("child_detail", child_id=child_id))

    @app.route("/children/<int:child_id>/generate", methods=["POST"])
    def generate_avatars(child_id):
        child = Child.query.get_or_404(child_id)

        if child.has_pending_generation:
            flash("Генерация уже запущена, подождите.", "warning")
            return redirect(url_for("child_detail", child_id=child_id))

        try:
            redis_client().ping()
        except Exception as exc:
            flash(f"Redis недоступен, не удалось поставить генерацию в очередь: {exc}", "danger")
            return redirect(url_for("child_detail", child_id=child_id))

        queued = 0
        failed = 0
        for style in app.config["CARTOON_STYLES"]:
            avatar = CartoonAvatar(
                child_id=child_id,
                style_name=style["name"],
                status="pending",
            )
            db.session.add(avatar)
            db.session.flush()

            try:
                job_id = enqueue_avatar_job(avatar.id)
                avatar.task_id = f"redis:{job_id}"
                queued += 1
            except Exception as exc:
                avatar.status = "failed"
                app.logger.error(
                    "Avatar enqueue failed avatar=%d child=%d style=%s: %s",
                    avatar.id,
                    child_id,
                    style["name"],
                    exc,
                )
                failed += 1

        db.session.commit()
        if queued > 0:
            flash(
                f"Генерация аватаров поставлена в очередь: {queued}. Ошибок постановки: {failed}.",
                "success" if failed == 0 else "warning",
            )
        else:
            flash("Не удалось поставить генерацию аватаров в очередь.", "danger")
        return redirect(url_for("child_detail", child_id=child_id))

    @app.route("/children/<int:child_id>/select/<int:avatar_id>", methods=["POST"])
    def select_avatar(child_id, avatar_id):
        child = Child.query.get_or_404(child_id)

        for av in child.avatars:
            av.is_selected = False

        avatar = CartoonAvatar.query.get_or_404(avatar_id)
        if avatar.child_id != child_id:
            abort(403)

        avatar.is_selected = True
        db.session.commit()

        flash(f"Выбран аватар в стиле «{avatar.style_name}»!", "success")
        return redirect(url_for("child_detail", child_id=child_id))

    @app.route("/children/<int:child_id>/delete", methods=["POST"])
    def delete_child(child_id):
        child = Child.query.get_or_404(child_id)
        photo_path = os.path.join(app.config["UPLOAD_FOLDER"], child.photo_filename)
        if os.path.exists(photo_path):
            os.remove(photo_path)
        db.session.delete(child)
        db.session.commit()
        flash(f"Ребёнок «{child.name}» удалён.", "success")
        return redirect(url_for("index"))

    # -------------------------------------------------------------- characters

    @app.route("/characters")
    def characters():
        chars = Character.query.order_by(Character.created_at.desc()).all()
        return render_template("characters.html", characters=chars)

    @app.route("/characters/add", methods=["POST"])
    def add_character():
        name = request.form.get("name", "").strip()
        if not name:
            flash("Введите имя персонажа.", "danger")
            return redirect(url_for("characters"))

        description = request.form.get("description", "").strip() or None
        voice_id = request.form.get("voice_id", "").strip() or None

        image_filename = None
        image = request.files.get("image")
        if image and image.filename != "":
            if not allowed_file(image.filename):
                flash("Допустимые форматы: JPG, PNG, WEBP.", "danger")
                return redirect(url_for("characters"))
            ext = image.filename.rsplit(".", 1)[1].lower()
            image_filename = f"{uuid.uuid4().hex}.{ext}"
            image.save(os.path.join(app.config["UPLOAD_FOLDER"], image_filename))

        char = Character(
            name=name,
            description=description,
            image_filename=image_filename,
            voice_id=voice_id,
        )
        db.session.add(char)
        db.session.commit()

        flash(f"Персонаж «{name}» добавлен!", "success")
        return redirect(url_for("character_detail", char_id=char.id))

    @app.route("/characters/<int:char_id>")
    def character_detail(char_id):
        char = Character.query.get_or_404(char_id)
        return render_template("character.html", char=char)

    @app.route("/characters/<int:char_id>/edit", methods=["POST"])
    def edit_character(char_id):
        char = Character.query.get_or_404(char_id)

        name = request.form.get("name", "").strip()
        if name:
            char.name = name
        char.description = request.form.get("description", "").strip() or None
        char.voice_id = request.form.get("voice_id", "").strip() or None

        image = request.files.get("image")
        if image and image.filename != "":
            if not allowed_file(image.filename):
                flash("Допустимые форматы: JPG, PNG, WEBP.", "danger")
                return redirect(url_for("character_detail", char_id=char_id))
            # remove old file
            if char.image_filename:
                old_path = os.path.join(app.config["UPLOAD_FOLDER"], char.image_filename)
                if os.path.exists(old_path):
                    os.remove(old_path)
            ext = image.filename.rsplit(".", 1)[1].lower()
            char.image_filename = f"{uuid.uuid4().hex}.{ext}"
            image.save(os.path.join(app.config["UPLOAD_FOLDER"], char.image_filename))

        db.session.commit()
        flash("Персонаж обновлён.", "success")
        return redirect(url_for("character_detail", char_id=char_id))

    @app.route("/characters/<int:char_id>/delete", methods=["POST"])
    def delete_character(char_id):
        char = Character.query.get_or_404(char_id)
        if char.image_filename:
            path = os.path.join(app.config["UPLOAD_FOLDER"], char.image_filename)
            if os.path.exists(path):
                os.remove(path)
        db.session.delete(char)
        db.session.commit()
        flash(f"Персонаж «{char.name}» удалён.", "success")
        return redirect(url_for("characters"))

    # ---------------------------------------------------------------- cartoons

    @app.route("/cartoons")
    def cartoons_list():
        items = Cartoon.query.order_by(Cartoon.created_at.desc()).all()
        return render_template("cartoons.html", cartoons=items)

    # ---------------------------------------------------------------- templates

    @app.route("/templates")
    def templates_list():
        root = templates_root_dir()
        template_dirs = []
        if os.path.isdir(root):
            for entry_name in sorted(os.listdir(root), key=lambda name: name.lower()):
                entry_path = os.path.join(root, entry_name)
                if not os.path.isdir(entry_path):
                    continue
                fragments_count = len(
                    [
                        file_name
                        for file_name in os.listdir(entry_path)
                        if (
                            os.path.isfile(os.path.join(entry_path, file_name))
                            and not file_name.startswith(".")
                        )
                    ]
                )
                template_dirs.append(
                    {
                        "name": entry_name,
                        "fragments_count": fragments_count,
                    }
                )
        return render_template("templates_list.html", template_dirs=template_dirs)

    @app.route("/templates/<template_name>")
    def template_detail(template_name):
        template_dir = safe_template_dir(template_name)
        if not template_dir:
            abort(404)

        filenames = [
            file_name
            for file_name in os.listdir(template_dir)
            if (
                os.path.isfile(os.path.join(template_dir, file_name))
                and not file_name.startswith(".")
            )
        ]
        filenames.sort(key=template_fragment_sort_key)
        prompts_by_file = load_template_prompts(template_dir)
        options3_by_file = load_template3_options(template_dir)
        jobs_by_file = load_template_jobs(template_dir)
        pixverse_cache = load_template_pixverse_cache(template_dir)

        video_exts = {"mp4", "mov", "webm", "m4v", "avi", "mkv"}
        image_exts = {"jpg", "jpeg", "png", "webp", "gif"}
        audio_exts = {"mp3", "wav", "ogg", "m4a", "aac"}

        fragments = []
        mask_timeline_by_file: dict[str, dict] = {}
        for file_name in filenames:
            ext = os.path.splitext(file_name)[1].lstrip(".").lower()
            media_type = "file"
            if ext in video_exts:
                media_type = "video"
            elif ext in image_exts:
                media_type = "image"
            elif ext in audio_exts:
                media_type = "audio"

            fragments.append(
                {
                    "name": file_name,
                    "url": url_for("static", filename=f"templates/{template_name}/{file_name}"),
                    "media_type": media_type,
                    "prompt": prompts_by_file.get(file_name, ""),
                    "job": jobs_by_file.get(file_name, {}),
                    "detected_masks": (
                        (pixverse_cache.get(file_name) or {}).get("detected_masks", [])
                        if media_type == "video"
                        else []
                    ),
                    "detected_keyframe_id": (
                        (pixverse_cache.get(file_name) or {}).get("keyframe_id")
                        if media_type == "video"
                        else None
                    ),
                }
            )
            if media_type == "video":
                timeline = (pixverse_cache.get(file_name) or {}).get("detected_masks_timeline", {})
                if isinstance(timeline, dict):
                    mask_timeline_by_file[file_name] = timeline

        return render_template(
            "template_detail.html",
            template_name=template_name,
            fragments=fragments,
            mask_timeline_by_file=mask_timeline_by_file,
        )

    @app.route("/templates/<template_name>/prompt", methods=["POST"])
    def save_template_fragment_prompt(template_name):
        template_dir = safe_template_dir(template_name)
        if not template_dir:
            abort(404)

        file_name = (request.form.get("file_name") or "").strip()
        prompt = (request.form.get("prompt") or "").strip()
        if not file_name:
            flash("Файл шаблона не указан.", "danger")
            return redirect(url_for("template_detail", template_name=template_name))
        if file_name.startswith("."):
            flash("Недопустимое имя файла.", "danger")
            return redirect(url_for("template_detail", template_name=template_name))

        file_path = os.path.abspath(os.path.join(template_dir, file_name))
        template_dir_abs = os.path.abspath(template_dir)
        try:
            if os.path.commonpath([template_dir_abs, file_path]) != template_dir_abs:
                flash("Недопустимый путь к файлу.", "danger")
                return redirect(url_for("template_detail", template_name=template_name))
        except ValueError:
            flash("Недопустимый путь к файлу.", "danger")
            return redirect(url_for("template_detail", template_name=template_name))

        if not os.path.isfile(file_path):
            flash("Файл шаблона не найден.", "danger")
            return redirect(url_for("template_detail", template_name=template_name))
        if is_mask_detection_busy(template_dir, file_name):
            flash("Идёт асинхронное определение масок для этого видео. Дождитесь завершения.", "warning")
            return redirect(url_for("template_detail", template_name=template_name))

        prompts = load_template_prompts(template_dir)
        if prompt:
            prompts[file_name] = prompt
        else:
            prompts.pop(file_name, None)
        save_template_prompts(template_dir, prompts)

        flash(f"Промпт для «{file_name}» сохранён.", "success")
        return redirect(url_for("template_detail", template_name=template_name))

    @app.route("/templates/<template_name>/apply", methods=["POST"])
    def apply_template_fragment_prompt(template_name):
        template_dir = safe_template_dir(template_name)
        if not template_dir:
            abort(404)

        file_name = (request.form.get("file_name") or "").strip()
        prompt = (request.form.get("prompt") or "").strip()
        apply_mode = (request.form.get("apply_mode") or "modify").strip().lower()
        if apply_mode not in {"modify", "swap"}:
            apply_mode = "modify"
        if not file_name:
            flash("Файл шаблона не указан.", "danger")
            return redirect(url_for("template_detail", template_name=template_name))
        if file_name.startswith("."):
            flash("Недопустимое имя файла.", "danger")
            return redirect(url_for("template_detail", template_name=template_name))
        if not prompt:
            flash("Промпт не должен быть пустым.", "danger")
            return redirect(url_for("template_detail", template_name=template_name))
        if not app.config.get("PIXVERSE_AI_API_KEY"):
            flash("PIXVERSE_AI_API_KEY не задан в .env.", "danger")
            return redirect(url_for("template_detail", template_name=template_name))

        file_path = os.path.abspath(os.path.join(template_dir, file_name))
        template_dir_abs = os.path.abspath(template_dir)
        try:
            if os.path.commonpath([template_dir_abs, file_path]) != template_dir_abs:
                flash("Недопустимый путь к файлу.", "danger")
                return redirect(url_for("template_detail", template_name=template_name))
        except ValueError:
            flash("Недопустимый путь к файлу.", "danger")
            return redirect(url_for("template_detail", template_name=template_name))
        if not os.path.isfile(file_path):
            flash("Файл шаблона не найден.", "danger")
            return redirect(url_for("template_detail", template_name=template_name))
        if is_mask_detection_busy(template_dir, file_name):
            flash("Идёт асинхронное определение масок для этого видео. Другие действия временно недоступны.", "warning")
            return redirect(url_for("template_detail", template_name=template_name))

        prompts = load_template_prompts(template_dir)
        prompts[file_name] = prompt
        save_template_prompts(template_dir, prompts)

        update_template_job(
            template_dir,
            file_name,
            status="queued",
            message=f"Задача {apply_mode.upper()} добавлена в очередь Redis",
            prompt=prompt,
            output_url="",
            operation=apply_mode,
        )

        try:
            enqueue_pixverse_job(
                template_name,
                file_name,
                prompt,
                mode=apply_mode,
            )
        except Exception as exc:
            update_template_job(
                template_dir,
                file_name,
                status="failed",
                message=f"Ошибка постановки в Redis: {exc}",
            )
            flash(f"Не удалось поставить задачу в Redis: {exc}", "danger")
            return redirect(url_for("template_detail", template_name=template_name))

        flash(
            f"Фрагмент «{file_name}» отправлен в очередь PixVerse (режим: {apply_mode.upper()}).",
            "success",
        )
        return redirect(url_for("template_detail", template_name=template_name))

    @app.route("/templates/<template_name>/detect-masks", methods=["POST"])
    def detect_template_masks(template_name):
        template_dir = safe_template_dir(template_name)
        if not template_dir:
            abort(404)
        if not app.config.get("PIXVERSE_AI_API_KEY"):
            flash("PIXVERSE_AI_API_KEY не задан в .env.", "danger")
            return redirect(url_for("template_detail", template_name=template_name))

        file_name = (request.form.get("file_name") or "").strip()
        aggressive = (request.form.get("aggressive_mask_search") or "").strip().lower() in {"1", "true", "on", "yes"}
        if not file_name:
            flash("Файл шаблона не указан.", "danger")
            return redirect(url_for("template_detail", template_name=template_name))
        if file_name.startswith("."):
            flash("Недопустимое имя файла.", "danger")
            return redirect(url_for("template_detail", template_name=template_name))

        file_path = os.path.abspath(os.path.join(template_dir, file_name))
        template_dir_abs = os.path.abspath(template_dir)
        try:
            if os.path.commonpath([template_dir_abs, file_path]) != template_dir_abs:
                flash("Недопустимый путь к файлу.", "danger")
                return redirect(url_for("template_detail", template_name=template_name))
        except ValueError:
            flash("Недопустимый путь к файлу.", "danger")
            return redirect(url_for("template_detail", template_name=template_name))
        if not os.path.isfile(file_path):
            flash("Файл шаблона не найден.", "danger")
            return redirect(url_for("template_detail", template_name=template_name))
        if is_mask_detection_busy(template_dir, file_name):
            flash("Определение масок уже запущено для этого видео.", "info")
            return redirect(url_for("template_detail", template_name=template_name))

        ext = os.path.splitext(file_name)[1].lstrip(".").lower()
        video_exts = {"mp4", "mov", "webm", "m4v", "avi", "mkv"}
        if ext not in video_exts:
            flash("Определение масок поддерживается только для видео.", "warning")
            return redirect(url_for("template_detail", template_name=template_name))

        update_template_job(
            template_dir,
            file_name,
            status="queued",
            message=(
                "Задача определения масок (агрессивный режим) добавлена в очередь Redis."
                if aggressive
                else "Задача определения масок добавлена в очередь Redis."
            ),
            operation="detect_masks",
        )
        try:
            enqueue_pixverse_job(
                template_name,
                file_name,
                "",
                mode="detect_masks",
                options={"aggressive": aggressive},
            )
        except Exception as exc:
            update_template_job(
                template_dir,
                file_name,
                status="failed",
                message=f"Ошибка постановки в Redis: {exc}",
                operation="detect_masks",
            )
            flash(f"Не удалось поставить задачу определения масок в Redis: {exc}", "danger")
            return redirect(url_for("template_detail", template_name=template_name))

        flash(
            f"Определение масок для «{file_name}» запущено асинхронно"
            + (" (агрессивный режим)." if aggressive else "."),
            "success",
        )
        return redirect(url_for("template_detail", template_name=template_name))

    @app.route("/api/templates/<template_name>/jobs")
    def api_template_jobs(template_name):
        template_dir = safe_template_dir(template_name)
        if not template_dir:
            return jsonify({"ok": False, "error": "template not found"}), 404
        return jsonify({"ok": True, "jobs": load_template_jobs(template_dir)})

    @app.route("/api/templates/<template_name>/mask-second")
    def api_template_mask_second(template_name):
        template_dir = safe_template_dir(template_name)
        if not template_dir:
            return jsonify({"ok": False, "error": "template not found"}), 404
        if not app.config.get("PIXVERSE_AI_API_KEY"):
            return jsonify({"ok": False, "error": "PIXVERSE_AI_API_KEY not set"}), 400

        file_name = (request.args.get("file_name") or "").strip()
        second_raw = (request.args.get("second") or "").strip()
        if not file_name:
            return jsonify({"ok": False, "error": "file_name required"}), 400
        try:
            second = max(0, int(second_raw))
        except Exception:
            return jsonify({"ok": False, "error": "invalid second"}), 400
        aggressive = (request.args.get("aggressive") or "").strip().lower() in {"1", "true", "yes", "on"}

        file_path = os.path.abspath(os.path.join(template_dir, file_name))
        template_dir_abs = os.path.abspath(template_dir)
        try:
            if os.path.commonpath([template_dir_abs, file_path]) != template_dir_abs:
                return jsonify({"ok": False, "error": "invalid path"}), 400
        except ValueError:
            return jsonify({"ok": False, "error": "invalid path"}), 400
        if not os.path.isfile(file_path):
            return jsonify({"ok": False, "error": "file not found"}), 404
        ext = os.path.splitext(file_name)[1].lstrip(".").lower()
        if ext not in {"mp4", "mov", "webm", "m4v", "avi", "mkv"}:
            return jsonify({"ok": False, "error": "video only"}), 400

        try:
            entry = get_valid_pixverse_cache_entry(template_dir, file_name, file_path) or {}
            timeline = entry.get("detected_masks_timeline")
            if isinstance(timeline, dict) and timeline.get(str(second)):
                cached_frame = timeline[str(second)]
                if (
                    aggressive
                    and isinstance(cached_frame, dict)
                    and isinstance(cached_frame.get("masks"), list)
                ):
                    has_character_like = any(
                        is_character_like_mask_name(str(m.get("mask_name", "")))
                        for m in cached_frame.get("masks", [])
                        if isinstance(m, dict)
                    )
                    if has_character_like:
                        return jsonify({"ok": True, "file_name": file_name, "frame": cached_frame, "cached": True})
                else:
                    return jsonify({"ok": True, "file_name": file_name, "frame": cached_frame, "cached": True})
            frame = detect_masks_for_video_second(
                template_dir,
                file_name,
                file_path,
                second,
                aggressive=aggressive,
                force=False,
            )
            return jsonify({"ok": True, "file_name": file_name, "frame": frame, "cached": False})
        except Exception as exc:
            app.logger.error(
                "Mask second API failed template=%s file=%s second=%s: %s",
                template_name,
                file_name,
                second,
                exc,
            )
            return jsonify({"ok": False, "error": str(exc)}), 500

    @app.route("/templates2")
    def templates2_list():
        root = templates2_root_dir()
        template_dirs = []
        if os.path.isdir(root):
            for entry_name in sorted(os.listdir(root), key=lambda name: name.lower()):
                entry_path = os.path.join(root, entry_name)
                if not os.path.isdir(entry_path):
                    continue
                fragments_count = len(
                    [
                        file_name
                        for file_name in os.listdir(entry_path)
                        if (
                            os.path.isfile(os.path.join(entry_path, file_name))
                            and not file_name.startswith(".")
                        )
                    ]
                )
                template_dirs.append(
                    {
                        "name": entry_name,
                        "fragments_count": fragments_count,
                    }
                )
        return render_template("templates2_list.html", template_dirs=template_dirs)

    @app.route("/templates2/<template_name>")
    def template2_detail(template_name):
        template_dir = safe_template2_dir(template_name)
        if not template_dir:
            abort(404)

        filenames = [
            file_name
            for file_name in os.listdir(template_dir)
            if (
                os.path.isfile(os.path.join(template_dir, file_name))
                and not file_name.startswith(".")
            )
        ]
        filenames.sort(key=template_fragment_sort_key)
        prompts_by_file = load_template_prompts(template_dir)
        options3_by_file = load_template3_options(template_dir)
        jobs_by_file = load_template_jobs(template_dir)
        pixverse_cache = load_template_pixverse_cache(template_dir)

        video_exts = {"mp4", "mov", "webm", "m4v", "avi", "mkv"}
        image_exts = {"jpg", "jpeg", "png", "webp", "gif"}
        audio_exts = {"mp3", "wav", "ogg", "m4a", "aac"}

        fragments = []
        mask_timeline_by_file: dict[str, dict] = {}
        for file_name in filenames:
            ext = os.path.splitext(file_name)[1].lstrip(".").lower()
            media_type = "file"
            if ext in video_exts:
                media_type = "video"
            elif ext in image_exts:
                media_type = "image"
            elif ext in audio_exts:
                media_type = "audio"

            fragments.append(
                {
                    "name": file_name,
                    "url": url_for("static", filename=f"templates2/{template_name}/{file_name}"),
                    "media_type": media_type,
                    "prompt": prompts_by_file.get(file_name, ""),
                    "job": jobs_by_file.get(file_name, {}),
                    "detected_masks": (
                        (pixverse_cache.get(file_name) or {}).get("detected_masks", [])
                        if media_type == "video"
                        else []
                    ),
                    "detected_keyframe_id": (
                        (pixverse_cache.get(file_name) or {}).get("keyframe_id")
                        if media_type == "video"
                        else None
                    ),
                }
            )
            if media_type == "video":
                timeline = (pixverse_cache.get(file_name) or {}).get("detected_masks_timeline", {})
                if isinstance(timeline, dict):
                    mask_timeline_by_file[file_name] = timeline

        return render_template(
            "template2_detail.html",
            template_name=template_name,
            fragments=fragments,
            mask_timeline_by_file=mask_timeline_by_file,
        )

    @app.route("/templates2/<template_name>/prompt", methods=["POST"])
    def save_template2_fragment_prompt(template_name):
        template_dir = safe_template2_dir(template_name)
        if not template_dir:
            abort(404)

        file_name = (request.form.get("file_name") or "").strip()
        prompt = (request.form.get("prompt") or "").strip()
        if not file_name:
            flash("Файл шаблона не указан.", "danger")
            return redirect(url_for("template2_detail", template_name=template_name))
        if file_name.startswith("."):
            flash("Недопустимое имя файла.", "danger")
            return redirect(url_for("template2_detail", template_name=template_name))

        file_path = os.path.abspath(os.path.join(template_dir, file_name))
        template_dir_abs = os.path.abspath(template_dir)
        try:
            if os.path.commonpath([template_dir_abs, file_path]) != template_dir_abs:
                flash("Недопустимый путь к файлу.", "danger")
                return redirect(url_for("template2_detail", template_name=template_name))
        except ValueError:
            flash("Недопустимый путь к файлу.", "danger")
            return redirect(url_for("template2_detail", template_name=template_name))

        if not os.path.isfile(file_path):
            flash("Файл шаблона не найден.", "danger")
            return redirect(url_for("template2_detail", template_name=template_name))

        prompts = load_template_prompts(template_dir)
        if prompt:
            prompts[file_name] = prompt
        else:
            prompts.pop(file_name, None)
        save_template_prompts(template_dir, prompts)

        flash(f"Промпт для «{file_name}» сохранён.", "success")
        return redirect(url_for("template2_detail", template_name=template_name))

    @app.route("/templates2/<template_name>/apply", methods=["POST"])
    def apply_template2_fragment_prompt(template_name):
        template_dir = safe_template2_dir(template_name)
        if not template_dir:
            abort(404)

        file_name = (request.form.get("file_name") or "").strip()
        prompt = (request.form.get("prompt") or "").strip()
        if not file_name:
            flash("Файл шаблона не указан.", "danger")
            return redirect(url_for("template2_detail", template_name=template_name))
        if file_name.startswith("."):
            flash("Недопустимое имя файла.", "danger")
            return redirect(url_for("template2_detail", template_name=template_name))
        if not prompt:
            flash("Промпт не должен быть пустым.", "danger")
            return redirect(url_for("template2_detail", template_name=template_name))
        if not app.config.get("ELEVENLABS_API_KEY"):
            flash("ELEVENLABS_API_KEY не задан в .env.", "danger")
            return redirect(url_for("template2_detail", template_name=template_name))

        file_path = os.path.abspath(os.path.join(template_dir, file_name))
        template_dir_abs = os.path.abspath(template_dir)
        try:
            if os.path.commonpath([template_dir_abs, file_path]) != template_dir_abs:
                flash("Недопустимый путь к файлу.", "danger")
                return redirect(url_for("template2_detail", template_name=template_name))
        except ValueError:
            flash("Недопустимый путь к файлу.", "danger")
            return redirect(url_for("template2_detail", template_name=template_name))
        if not os.path.isfile(file_path):
            flash("Файл шаблона не найден.", "danger")
            return redirect(url_for("template2_detail", template_name=template_name))

        prompts = load_template_prompts(template_dir)
        prompts[file_name] = prompt
        save_template_prompts(template_dir, prompts)

        update_template_job(
            template_dir,
            file_name,
            status="queued",
            message="Задача добавлена в очередь Redis",
            prompt=prompt,
            output_url="",
            event_name="template2_job_update",
            room_prefix="template2",
        )

        try:
            enqueue_template2_job(template_name, file_name, prompt)
        except Exception as exc:
            update_template_job(
                template_dir,
                file_name,
                status="failed",
                message=f"Ошибка постановки в Redis: {exc}",
                event_name="template2_job_update",
                room_prefix="template2",
            )
            flash(f"Не удалось поставить задачу в Redis: {exc}", "danger")
            return redirect(url_for("template2_detail", template_name=template_name))

        flash(f"Фрагмент «{file_name}» отправлен в очередь обработки ElevenLabs Creative.", "success")
        return redirect(url_for("template2_detail", template_name=template_name))

    @app.route("/templates2/<template_name>/detect-masks", methods=["POST"])
    def detect_template2_masks(template_name):
        template_dir = safe_template2_dir(template_name)
        if not template_dir:
            abort(404)
        if not app.config.get("PIXVERSE_AI_API_KEY"):
            flash("PIXVERSE_AI_API_KEY не задан в .env.", "danger")
            return redirect(url_for("template2_detail", template_name=template_name))

        file_name = (request.form.get("file_name") or "").strip()
        if not file_name:
            flash("Файл шаблона не указан.", "danger")
            return redirect(url_for("template2_detail", template_name=template_name))
        if file_name.startswith("."):
            flash("Недопустимое имя файла.", "danger")
            return redirect(url_for("template2_detail", template_name=template_name))

        file_path = os.path.abspath(os.path.join(template_dir, file_name))
        template_dir_abs = os.path.abspath(template_dir)
        try:
            if os.path.commonpath([template_dir_abs, file_path]) != template_dir_abs:
                flash("Недопустимый путь к файлу.", "danger")
                return redirect(url_for("template2_detail", template_name=template_name))
        except ValueError:
            flash("Недопустимый путь к файлу.", "danger")
            return redirect(url_for("template2_detail", template_name=template_name))
        if not os.path.isfile(file_path):
            flash("Файл шаблона не найден.", "danger")
            return redirect(url_for("template2_detail", template_name=template_name))

        ext = os.path.splitext(file_name)[1].lstrip(".").lower()
        video_exts = {"mp4", "mov", "webm", "m4v", "avi", "mkv"}
        if ext not in video_exts:
            flash("Определение масок поддерживается только для видео.", "warning")
            return redirect(url_for("template2_detail", template_name=template_name))

        try:
            redis_client().ping()
        except Exception:
            pass

        duration, _fps = probe_video_timing(file_path)
        total_seconds = max(1, int(math.ceil(duration))) if duration > 0 else 1
        detected = 0
        skipped = 0
        failed = 0
        for second in range(total_seconds):
            try:
                existing = get_valid_pixverse_cache_entry(template_dir, file_name, file_path) or {}
                timeline = existing.get("detected_masks_timeline")
                if isinstance(timeline, dict) and timeline.get(str(second)):
                    skipped += 1
                    continue
                detect_masks_for_video_second(
                    template_dir,
                    file_name,
                    file_path,
                    second,
                    force=False,
                )
                detected += 1
            except Exception as exc:
                failed += 1
                app.logger.error(
                    "Mask detect failed template2=%s file=%s second=%s: %s",
                    template_name,
                    file_name,
                    second,
                    exc,
                )

        flash(
            f"Маски для «{file_name}» по секундам: новых {detected}, уже были {skipped}, ошибок {failed}.",
            "success" if failed == 0 else "warning",
        )
        return redirect(url_for("template2_detail", template_name=template_name))

    @app.route("/api/templates2/<template_name>/jobs")
    def api_template2_jobs(template_name):
        template_dir = safe_template2_dir(template_name)
        if not template_dir:
            return jsonify({"ok": False, "error": "template not found"}), 404
        return jsonify({"ok": True, "jobs": load_template_jobs(template_dir)})

    @app.route("/api/templates2/<template_name>/mask-second")
    def api_template2_mask_second(template_name):
        template_dir = safe_template2_dir(template_name)
        if not template_dir:
            return jsonify({"ok": False, "error": "template not found"}), 404
        if not app.config.get("PIXVERSE_AI_API_KEY"):
            return jsonify({"ok": False, "error": "PIXVERSE_AI_API_KEY not set"}), 400

        file_name = (request.args.get("file_name") or "").strip()
        second_raw = (request.args.get("second") or "").strip()
        if not file_name:
            return jsonify({"ok": False, "error": "file_name required"}), 400
        try:
            second = max(0, int(second_raw))
        except Exception:
            return jsonify({"ok": False, "error": "invalid second"}), 400

        file_path = os.path.abspath(os.path.join(template_dir, file_name))
        template_dir_abs = os.path.abspath(template_dir)
        try:
            if os.path.commonpath([template_dir_abs, file_path]) != template_dir_abs:
                return jsonify({"ok": False, "error": "invalid path"}), 400
        except ValueError:
            return jsonify({"ok": False, "error": "invalid path"}), 400
        if not os.path.isfile(file_path):
            return jsonify({"ok": False, "error": "file not found"}), 404
        ext = os.path.splitext(file_name)[1].lstrip(".").lower()
        if ext not in {"mp4", "mov", "webm", "m4v", "avi", "mkv"}:
            return jsonify({"ok": False, "error": "video only"}), 400

        try:
            entry = get_valid_pixverse_cache_entry(template_dir, file_name, file_path) or {}
            timeline = entry.get("detected_masks_timeline")
            if isinstance(timeline, dict) and timeline.get(str(second)):
                return jsonify({"ok": True, "file_name": file_name, "frame": timeline[str(second)], "cached": True})
            frame = detect_masks_for_video_second(
                template_dir,
                file_name,
                file_path,
                second,
                force=False,
            )
            return jsonify({"ok": True, "file_name": file_name, "frame": frame, "cached": False})
        except Exception as exc:
            app.logger.error(
                "Mask second API failed template2=%s file=%s second=%s: %s",
                template_name,
                file_name,
                second,
                exc,
            )
            return jsonify({"ok": False, "error": str(exc)}), 500

    @app.route("/api/templates2/check-access")
    def api_templates2_check_access():
        try:
            client = elevenlabs_sdk_client()
            user = client.user.get()
            tier = None
            try:
                subscription = getattr(user, "subscription", None)
                tier = getattr(subscription, "tier", None) if subscription is not None else None
            except Exception:
                tier = None

            elevenlabs_sdk_preflight()
            return jsonify(
                {
                    "ok": True,
                    "message": "Доступ к ElevenLabs SDK и Studio API подтверждён.",
                    "tier": tier,
                }
            )
        except Exception as exc:
            return jsonify(
                {
                    "ok": False,
                    "message": str(exc),
                }
            ), 400

    @app.route("/templates3")
    def templates3_list():
        root = templates3_root_dir()
        template_dirs = []
        if os.path.isdir(root):
            for entry_name in sorted(os.listdir(root), key=lambda name: name.lower()):
                entry_path = os.path.join(root, entry_name)
                if not os.path.isdir(entry_path):
                    continue
                fragments_count = len(
                    [
                        file_name
                        for file_name in os.listdir(entry_path)
                        if os.path.isfile(os.path.join(entry_path, file_name)) and not file_name.startswith(".")
                    ]
                )
                template_dirs.append({"name": entry_name, "fragments_count": fragments_count})
        return render_template("templates3_list.html", template_dirs=template_dirs)

    @app.route("/templates3/<template_name>")
    def template3_detail(template_name):
        template_dir = safe_template3_dir(template_name)
        if not template_dir:
            abort(404)
        filenames = [
            file_name
            for file_name in os.listdir(template_dir)
            if os.path.isfile(os.path.join(template_dir, file_name)) and not file_name.startswith(".")
        ]
        filenames.sort(key=template_fragment_sort_key)
        prompts_by_file = load_template_prompts(template_dir)
        options3_by_file = load_template3_options(template_dir)
        jobs_by_file = load_template_jobs(template_dir)
        video_exts = {"mp4", "mov", "webm", "m4v", "avi", "mkv"}
        image_exts = {"jpg", "jpeg", "png", "webp", "gif"}
        audio_exts = {"mp3", "wav", "ogg", "m4a", "aac"}
        fragments = []
        for file_name in filenames:
            ext = os.path.splitext(file_name)[1].lstrip(".").lower()
            media_type = "file"
            if ext in video_exts:
                media_type = "video"
            elif ext in image_exts:
                media_type = "image"
            elif ext in audio_exts:
                media_type = "audio"
            fragments.append(
                {
                    "name": file_name,
                    "url": url_for("static", filename=f"templates3/{template_name}/{file_name}"),
                    "media_type": media_type,
                    "prompt": prompts_by_file.get(file_name, ""),
                    "options3": options3_by_file.get(file_name, {}),
                    "job": jobs_by_file.get(file_name, {}),
                }
            )
        avatar_options = []
        children = Child.query.order_by(Child.name.asc()).all()
        for child in children:
            selected = child.selected_avatar
            if not selected or selected.status != "completed":
                continue
            avatar_options.append(
                {
                    "avatar_id": selected.id,
                    "child_name": child.name,
                    "style_name": selected.style_name or "",
                    "image_url": (selected.image_url or "").strip(),
                }
            )
        return render_template(
            "template3_detail.html",
            template_name=template_name,
            fragments=fragments,
            avatar_options=avatar_options,
        )

    @app.route("/templates3/<template_name>/prompt", methods=["POST"])
    def save_template3_fragment_prompt(template_name):
        template_dir = safe_template3_dir(template_name)
        if not template_dir:
            abort(404)
        file_name = (request.form.get("file_name") or "").strip()
        prompt = (request.form.get("prompt") or "").strip()
        source_face_ref = (request.form.get("source_face_ref") or "").strip()
        child_avatar_id_raw = (request.form.get("child_avatar_id") or "").strip()
        negative_prompt = (request.form.get("negative_prompt") or "").strip()
        denoise_strength_raw = (request.form.get("denoise_strength") or "").strip()
        if not file_name or file_name.startswith("."):
            flash("Недопустимое имя файла.", "danger")
            return redirect(url_for("template3_detail", template_name=template_name))
        file_path = os.path.abspath(os.path.join(template_dir, file_name))
        template_dir_abs = os.path.abspath(template_dir)
        try:
            if os.path.commonpath([template_dir_abs, file_path]) != template_dir_abs:
                flash("Недопустимый путь к файлу.", "danger")
                return redirect(url_for("template3_detail", template_name=template_name))
        except ValueError:
            flash("Недопустимый путь к файлу.", "danger")
            return redirect(url_for("template3_detail", template_name=template_name))
        if not os.path.isfile(file_path):
            flash("Файл шаблона не найден.", "danger")
            return redirect(url_for("template3_detail", template_name=template_name))
        prompts = load_template_prompts(template_dir)
        if prompt:
            prompts[file_name] = prompt
        else:
            prompts.pop(file_name, None)
        save_template_prompts(template_dir, prompts)
        options3 = load_template3_options(template_dir)
        file_options = options3.get(file_name, {}) if isinstance(options3.get(file_name), dict) else {}
        file_options["source_face_ref"] = source_face_ref
        file_options["child_avatar_id"] = child_avatar_id_raw
        file_options["negative_prompt"] = negative_prompt
        file_options["denoise_strength"] = denoise_strength_raw
        options3[file_name] = file_options
        save_template3_options(template_dir, options3)
        flash(f"Промпт для «{file_name}» сохранён.", "success")
        return redirect(url_for("template3_detail", template_name=template_name))

    @app.route("/templates3/<template_name>/apply", methods=["POST"])
    def apply_template3_fragment_prompt(template_name):
        template_dir = safe_template3_dir(template_name)
        if not template_dir:
            abort(404)
        file_name = (request.form.get("file_name") or "").strip()
        prompt = (request.form.get("prompt") or "").strip()
        source_face_ref = (request.form.get("source_face_ref") or "").strip()
        child_avatar_id_raw = (request.form.get("child_avatar_id") or "").strip()
        negative_prompt = (request.form.get("negative_prompt") or "").strip()
        denoise_strength_raw = (request.form.get("denoise_strength") or "").strip()
        if not file_name or file_name.startswith("."):
            flash("Недопустимое имя файла.", "danger")
            return redirect(url_for("template3_detail", template_name=template_name))
        if not prompt:
            flash("Промпт не должен быть пустым.", "danger")
            return redirect(url_for("template3_detail", template_name=template_name))
        child_avatar_id = None
        if child_avatar_id_raw:
            try:
                child_avatar_id = int(child_avatar_id_raw)
            except Exception:
                flash("Некорректный выбор аватара ребёнка.", "danger")
                return redirect(url_for("template3_detail", template_name=template_name))
        if not child_avatar_id and not source_face_ref:
            flash("Выберите аватар ребёнка или укажите Source Face (URL/файл в папке шаблона).", "danger")
            return redirect(url_for("template3_detail", template_name=template_name))
        denoise_strength = None
        if denoise_strength_raw:
            try:
                denoise_strength = float(denoise_strength_raw)
                if denoise_strength < 0 or denoise_strength > 1:
                    raise ValueError()
            except Exception:
                flash("Denoise strength должен быть числом от 0 до 1.", "danger")
                return redirect(url_for("template3_detail", template_name=template_name))
        file_path = os.path.abspath(os.path.join(template_dir, file_name))
        template_dir_abs = os.path.abspath(template_dir)
        try:
            if os.path.commonpath([template_dir_abs, file_path]) != template_dir_abs:
                flash("Недопустимый путь к файлу.", "danger")
                return redirect(url_for("template3_detail", template_name=template_name))
        except ValueError:
            flash("Недопустимый путь к файлу.", "danger")
            return redirect(url_for("template3_detail", template_name=template_name))
        if not os.path.isfile(file_path):
            flash("Файл шаблона не найден.", "danger")
            return redirect(url_for("template3_detail", template_name=template_name))

        prompts = load_template_prompts(template_dir)
        prompts[file_name] = prompt
        save_template_prompts(template_dir, prompts)
        options3 = load_template3_options(template_dir)
        file_options = options3.get(file_name, {}) if isinstance(options3.get(file_name), dict) else {}
        file_options["source_face_ref"] = source_face_ref
        file_options["child_avatar_id"] = str(child_avatar_id) if child_avatar_id else ""
        file_options["negative_prompt"] = negative_prompt
        file_options["denoise_strength"] = denoise_strength_raw
        options3[file_name] = file_options
        save_template3_options(template_dir, options3)
        update_template_job(
            template_dir,
            file_name,
            status="queued",
            message="Задача добавлена в очередь Redis",
            prompt=prompt,
            output_url="",
            event_name="template3_job_update",
            room_prefix="template3",
        )
        try:
            enqueue_template3_job(
                template_name,
                file_name,
                prompt,
                source_face_ref,
                child_avatar_id,
                negative_prompt,
                denoise_strength,
            )
        except Exception as exc:
            update_template_job(
                template_dir,
                file_name,
                status="failed",
                message=f"Ошибка постановки в Redis: {exc}",
                event_name="template3_job_update",
                room_prefix="template3",
            )
            flash(f"Не удалось поставить задачу в Redis: {exc}", "danger")
            return redirect(url_for("template3_detail", template_name=template_name))
        flash(f"Фрагмент «{file_name}» отправлен в очередь Stable Diffusion.", "success")
        return redirect(url_for("template3_detail", template_name=template_name))

    @app.route("/api/templates3/<template_name>/jobs")
    def api_template3_jobs(template_name):
        template_dir = safe_template3_dir(template_name)
        if not template_dir:
            return jsonify({"ok": False, "error": "template not found"}), 404
        return jsonify({"ok": True, "jobs": load_template_jobs(template_dir)})

    @app.route("/api/templates3/check-access")
    def api_templates3_check_access():
        sd_base = (app.config.get("SD_WEBUI_BASE_URL") or "").rstrip("/")
        if not sd_base:
            return jsonify({"ok": False, "message": "SD_WEBUI_BASE_URL не задан"}), 400
        try:
            resp = requests.get(f"{sd_base}/sdapi/v1/progress", headers=sd_webui_headers(), timeout=20)
            resp.raise_for_status()
            return jsonify({"ok": True, "message": "Доступ к SD WebUI API подтверждён."})
        except Exception as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400

    @app.route("/templates4")
    def templates4_list():
        root = templates4_root_dir()
        template_dirs = []
        if os.path.isdir(root):
            for entry_name in sorted(os.listdir(root), key=lambda name: name.lower()):
                entry_path = os.path.join(root, entry_name)
                if not os.path.isdir(entry_path):
                    continue
                template_dirs.append({"name": entry_name})
        return render_template("templates4_list.html", template_dirs=template_dirs)

    @app.route("/templates4/create", methods=["POST"])
    def create_template4_project():
        root = templates4_root_dir()
        os.makedirs(root, exist_ok=True)
        project_name = (request.form.get("project_name") or "").strip()
        if not project_name:
            project_name = f"project_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        if project_name in {".", ".."}:
            flash("Некорректное имя проекта.", "danger")
            return redirect(url_for("templates4_list"))
        if os.sep in project_name or (os.altsep and os.altsep in project_name):
            flash("Имя проекта не должно содержать символы пути.", "danger")
            return redirect(url_for("templates4_list"))
        project_dir = os.path.abspath(os.path.join(root, project_name))
        root_abs = os.path.abspath(root)
        try:
            if os.path.commonpath([root_abs, project_dir]) != root_abs:
                flash("Некорректное имя проекта.", "danger")
                return redirect(url_for("templates4_list"))
        except ValueError:
            flash("Некорректное имя проекта.", "danger")
            return redirect(url_for("templates4_list"))
        if os.path.exists(project_dir):
            flash("Проект с таким именем уже существует.", "warning")
            return redirect(url_for("templates4_list"))
        os.makedirs(project_dir, exist_ok=True)
        save_template4_scenes(project_dir, [])
        flash(f"Проект «{project_name}» создан.", "success")
        return redirect(url_for("template4_detail", template_name=project_name))

    @app.route("/templates4/<template_name>/delete", methods=["POST"])
    def delete_template4_project(template_name):
        template_dir = safe_template4_dir(template_name)
        if not template_dir:
            abort(404)
        try:
            shutil.rmtree(template_dir)
            flash(f"Проект «{template_name}» удалён.", "success")
        except Exception as exc:
            flash(f"Не удалось удалить проект: {exc}", "danger")
        return redirect(url_for("templates4_list"))

    @app.route("/templates4/<template_name>")
    def template4_detail(template_name):
        template_dir = safe_template4_dir(template_name)
        if not template_dir:
            abort(404)
        scenes = load_template4_scenes(template_dir)
        avatar_options = []
        for child in Child.query.order_by(Child.name.asc()).all():
            selected = child.selected_avatar
            if not selected or selected.status != "completed":
                continue
            avatar_options.append(
                {
                    "avatar_id": selected.id,
                    "child_name": child.name,
                    "style_name": selected.style_name or "",
                    "image_url": (selected.image_url or "").strip(),
                }
            )
        scene_image_options = []
        scene_audio_options = []
        assets_dir = os.path.join(template_dir, "assets")
        if os.path.isdir(assets_dir):
            for fn in sorted(os.listdir(assets_dir)):
                lower = fn.lower()
                if lower.endswith((".png", ".jpg", ".jpeg", ".webp")):
                    scene_image_options.append(f"assets/{fn}")
                if lower.endswith((".mp3", ".wav", ".ogg", ".m4a", ".aac")):
                    scene_audio_options.append(f"assets/{fn}")
        for sc in scenes:
            # Build per-scene visual candidates for image selection in step modal.
            image_candidates = []
            seen = set()
            base_ref = str(sc.get("image_ref") or "").strip()
            if base_ref:
                url = url_for("static", filename=f"templates4/{template_name}/{base_ref}")
                image_candidates.append({"ref": base_ref, "url": url, "label": "Текущий кадр сцены"})
                seen.add(base_ref)
            for ref in scene_image_options:
                if ref in seen:
                    continue
                image_candidates.append(
                    {
                        "ref": ref,
                        "url": url_for("static", filename=f"templates4/{template_name}/{ref}"),
                        "label": ref,
                    }
                )
                seen.add(ref)
            steps = sc.get("steps") if isinstance(sc.get("steps"), list) else []
            for st in reversed(steps):
                if str(st.get("status") or "").lower() != "completed":
                    continue
                out = str(st.get("output_url") or "").strip()
                if out.startswith(f"/static/templates4/{template_name}/") and out.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                    ref = out.replace(f"/static/templates4/{template_name}/", "", 1)
                    if ref not in seen:
                        image_candidates.insert(
                            0,
                            {
                                "ref": ref,
                                "url": out,
                                "label": f"Результат шага: {st.get('name') or st.get('id')}",
                            },
                        )
                        seen.add(ref)
            sc["image_candidates"] = image_candidates
            audio_candidates = []
            audio_ref = str(sc.get("audio_ref") or "").strip()
            if audio_ref:
                audio_candidates.append({"ref": audio_ref, "label": "Звук сцены"})
            for ref in scene_audio_options:
                if audio_ref and ref == audio_ref:
                    continue
                audio_candidates.append({"ref": ref, "label": ref})
            sc["audio_candidates"] = audio_candidates
        openai_image_models = []
        for m in [app.config.get("OPENAI_IMAGE_MODEL"), app.config.get("OPENAI_AVATAR_MODEL"), "gpt-image-1", "gpt-image-2"]:
            v = (m or "").strip()
            if v and v not in openai_image_models:
                openai_image_models.append(v)
        kling_models = []
        kling_default = (app.config.get("KLING_MODEL") or "").strip()
        kling_raw_list = os.environ.get("KLING_MODELS", "")
        if kling_raw_list:
            for item in kling_raw_list.split(","):
                val = item.strip()
                if val and val not in kling_models:
                    kling_models.append(val)
        if kling_default and kling_default not in kling_models:
            kling_models.insert(0, kling_default)
        return render_template(
            "template4_detail.html",
            template_name=template_name,
            scenes=scenes,
            avatar_options=avatar_options,
            scene_image_options=scene_image_options,
            scene_audio_options=scene_audio_options,
            openai_image_models=openai_image_models,
            kling_models=kling_models,
            max_video_seconds=int(app.config.get("TEMPLATE4_MAX_VIDEO_SECONDS", 10)),
        )

    @app.route("/templates4/<template_name>/scene", methods=["POST"])
    def create_template4_scene(template_name):
        template_dir = safe_template4_dir(template_name)
        if not template_dir:
            abort(404)
        image_file = request.files.get("scene_image")
        audio_file = request.files.get("scene_audio")
        prompt = ""
        child_avatar_id = None
        avatar_url = ""

        assets_dir = os.path.join(template_dir, "assets")
        os.makedirs(assets_dir, exist_ok=True)
        scene_id = uuid.uuid4().hex[:12]
        image_ref = ""
        if image_file and image_file.filename:
            img_ext = os.path.splitext(image_file.filename or "")[1].lower() or ".jpg"
            image_name = f"scene_{scene_id}{img_ext}"
            image_path = os.path.join(assets_dir, image_name)
            image_file.save(image_path)
            image_ref = f"assets/{image_name}"

        audio_name = ""
        duration_seconds = 8
        max_seconds = int(app.config.get("TEMPLATE4_MAX_VIDEO_SECONDS", 10))
        if audio_file and audio_file.filename:
            audio_ext = os.path.splitext(audio_file.filename or "")[1].lower() or ".mp3"
            audio_name = f"scene_{scene_id}{audio_ext}"
            audio_path = os.path.join(assets_dir, audio_name)
            audio_file.save(audio_path)
            duration_seconds = max(1, min(max_seconds, int(round(probe_audio_duration_seconds(audio_path) or 0)) or 1))

        scene = {
            "id": scene_id,
            "prompt": prompt,
            "image_ref": image_ref,
            "audio_ref": f"assets/{audio_name}" if audio_name else "",
            "child_avatar_id": child_avatar_id,
            "avatar_url": avatar_url,
            "kling_task_id": "",
            "status": "draft",
            "video_url": "",
            "message": "",
            "duration_seconds": duration_seconds,
            "steps": [],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        scenes = load_template4_scenes(template_dir)
        scenes.append(scene)
        save_template4_scenes(template_dir, scenes)
        flash("Сцена создана.", "success")
        return redirect(url_for("template4_detail", template_name=template_name))

    @app.route("/templates4/<template_name>/scenes/<scene_id>/steps", methods=["POST"])
    def create_template4_scene_step(template_name, scene_id):
        template_dir = safe_template4_dir(template_name)
        if not template_dir:
            abort(404)
        step_type = (request.form.get("step_type") or "").strip()
        step_prompt = (request.form.get("step_prompt") or "").strip()
        child_avatar_id_raw = (request.form.get("step_child_avatar_id") or "").strip()
        source_image_ref = (request.form.get("step_source_image_ref") or "").strip()
        source_audio_ref = (request.form.get("step_source_audio_ref") or "").strip()
        step_model = (request.form.get("step_model") or "").strip()
        audio_file = request.files.get("step_audio_file")
        if step_type not in {"add_avatar_to_first_frame", "edit_first_frame", "generate_video"}:
            flash("Некорректный тип шага.", "danger")
            return redirect(url_for("template4_detail", template_name=template_name))
        scenes, scene, _ = get_scene_and_step(template_dir, scene_id)
        if not scene:
            flash("Сцена не найдена.", "danger")
            return redirect(url_for("template4_detail", template_name=template_name))
        steps = scene.get("steps") if isinstance(scene.get("steps"), list) else []
        step_id = uuid.uuid4().hex[:10]
        avatar_url = ""
        child_avatar_id = None
        if child_avatar_id_raw:
            try:
                child_avatar_id = int(child_avatar_id_raw)
                avatar = CartoonAvatar.query.get(child_avatar_id)
                if avatar and avatar.status == "completed":
                    avatar_url = (avatar.image_url or "").strip()
            except Exception:
                child_avatar_id = None
        if step_type in {"add_avatar_to_first_frame", "edit_first_frame"} and not source_image_ref:
            flash("Для шага нужно выбрать кадр.", "danger")
            return redirect(url_for("template4_detail", template_name=template_name))
        if step_type == "add_avatar_to_first_frame" and not child_avatar_id:
            flash("Для шага добавления аватара нужно выбрать аватар ребенка.", "danger")
            return redirect(url_for("template4_detail", template_name=template_name))
        if step_type == "generate_video" and not source_image_ref:
            flash("Для шага генерации видео нужно выбрать первый кадр.", "danger")
            return redirect(url_for("template4_detail", template_name=template_name))
        if step_type == "generate_video" and audio_file and audio_file.filename:
            assets_dir = os.path.join(template_dir, "assets")
            os.makedirs(assets_dir, exist_ok=True)
            audio_ext = os.path.splitext(audio_file.filename or "")[1].lower() or ".mp3"
            audio_name = f"step_{uuid.uuid4().hex[:10]}{audio_ext}"
            audio_path = os.path.join(assets_dir, audio_name)
            audio_file.save(audio_path)
            source_audio_ref = f"assets/{audio_name}"

        step = {
            "id": step_id,
            "name": f"{step_type}:{len(steps) + 1}",
            "type": step_type,
            "prompt": step_prompt,
            "child_avatar_id": child_avatar_id,
            "avatar_url": avatar_url,
            "source_image_ref": source_image_ref,
            "source_audio_ref": source_audio_ref,
            "model": step_model,
            "status": "draft",
            "message": "",
            "output_url": "",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        steps.append(step)
        scene["steps"] = steps
        scene["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_template4_scenes(template_dir, scenes)
        flash("Шаг добавлен.", "success")
        return redirect(url_for("template4_detail", template_name=template_name))

    @app.route("/templates4/<template_name>/scenes/<scene_id>/steps/<step_id>/edit", methods=["POST"])
    def edit_template4_scene_step(template_name, scene_id, step_id):
        template_dir = safe_template4_dir(template_name)
        if not template_dir:
            abort(404)
        step_type = (request.form.get("step_type") or "").strip()
        step_prompt = (request.form.get("step_prompt") or "").strip()
        child_avatar_id_raw = (request.form.get("step_child_avatar_id") or "").strip()
        source_image_ref = (request.form.get("step_source_image_ref") or "").strip()
        source_audio_ref = (request.form.get("step_source_audio_ref") or "").strip()
        step_model = (request.form.get("step_model") or "").strip()
        audio_file = request.files.get("step_audio_file")
        if step_type not in {"add_avatar_to_first_frame", "edit_first_frame", "generate_video"}:
            flash("Некорректный тип шага.", "danger")
            return redirect(url_for("template4_detail", template_name=template_name))
        scenes, scene, step = get_scene_and_step(template_dir, scene_id, step_id)
        if not scene or not step:
            flash("Шаг/сцена не найдены.", "danger")
            return redirect(url_for("template4_detail", template_name=template_name))

        avatar_url = ""
        child_avatar_id = None
        if child_avatar_id_raw:
            try:
                child_avatar_id = int(child_avatar_id_raw)
                avatar = CartoonAvatar.query.get(child_avatar_id)
                if avatar and avatar.status == "completed":
                    avatar_url = (avatar.image_url or "").strip()
            except Exception:
                child_avatar_id = None
        if step_type in {"add_avatar_to_first_frame", "edit_first_frame"} and not source_image_ref:
            flash("Для шага нужно выбрать кадр.", "danger")
            return redirect(url_for("template4_detail", template_name=template_name))
        if step_type == "add_avatar_to_first_frame" and not child_avatar_id:
            flash("Для шага добавления аватара нужно выбрать аватар ребенка.", "danger")
            return redirect(url_for("template4_detail", template_name=template_name))
        if step_type == "generate_video" and not source_image_ref:
            flash("Для шага генерации видео нужно выбрать первый кадр.", "danger")
            return redirect(url_for("template4_detail", template_name=template_name))
        if step_type == "generate_video" and audio_file and audio_file.filename:
            assets_dir = os.path.join(template_dir, "assets")
            os.makedirs(assets_dir, exist_ok=True)
            audio_ext = os.path.splitext(audio_file.filename or "")[1].lower() or ".mp3"
            audio_name = f"step_{uuid.uuid4().hex[:10]}{audio_ext}"
            audio_path = os.path.join(assets_dir, audio_name)
            audio_file.save(audio_path)
            source_audio_ref = f"assets/{audio_name}"

        step["name"] = f"{step_type}:{(scene.get('steps') or []).index(step) + 1}" if step in (scene.get("steps") or []) else step.get("name")
        step["type"] = step_type
        step["prompt"] = step_prompt
        step["child_avatar_id"] = child_avatar_id
        step["avatar_url"] = avatar_url
        step["source_image_ref"] = source_image_ref
        step["source_audio_ref"] = source_audio_ref
        step["model"] = step_model
        step["status"] = "draft"
        step["message"] = "Шаг изменен. Запустите заново."
        step["output_url"] = ""
        step["updated_at"] = datetime.now(timezone.utc).isoformat()
        scene["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_template4_scenes(template_dir, scenes)
        flash("Шаг обновлен.", "success")
        return redirect(url_for("template4_detail", template_name=template_name))

    @app.route("/templates4/<template_name>/scenes/<scene_id>/steps/<step_id>/run", methods=["POST"])
    def run_template4_scene_step_route(template_name, scene_id, step_id):
        ok, message = queue_template4_step_run(template_name, scene_id, step_id)
        if ok:
            flash(message, "success")
        else:
            flash(message, "danger")
        return redirect(url_for("template4_detail", template_name=template_name))

    def queue_template4_step_run(template_name: str, scene_id: str, step_id: str) -> tuple[bool, str]:
        template_dir = safe_template4_dir(template_name)
        if not template_dir:
            return False, "Шаблон не найден."
        scenes, scene, step = get_scene_and_step(template_dir, scene_id, step_id)
        if not scene or not step:
            return False, "Шаг/сцена не найдены."
        step["status"] = "queued"
        step["message"] = "Шаг добавлен в очередь"
        step["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_template4_scenes(template_dir, scenes)
        update_template_job(
            template_dir,
            scene_id,
            status=str(scene.get("status") or "draft"),
            message=step["message"],
            event_name="template4_job_update",
            room_prefix="template4",
        )
        try:
            enqueue_template4_job(template_name, scene_id, step_id=step_id)
        except Exception as exc:
            step["status"] = "failed"
            step["message"] = str(exc)
            step["updated_at"] = datetime.now(timezone.utc).isoformat()
            save_template4_scenes(template_dir, scenes)
            update_template_job(
                template_dir,
                scene_id,
                status=str(scene.get("status") or "draft"),
                message=step["message"],
                event_name="template4_job_update",
                room_prefix="template4",
            )
            return False, f"Не удалось поставить шаг в очередь: {exc}"
        return True, "Запуск шага поставлен в очередь."

    @app.route("/templates4/<template_name>/scenes/<scene_id>/steps/<step_id>/delete", methods=["POST"])
    def delete_template4_scene_step(template_name, scene_id, step_id):
        template_dir = safe_template4_dir(template_name)
        if not template_dir:
            abort(404)
        scenes, scene, _step = get_scene_and_step(template_dir, scene_id, step_id)
        if not scene:
            flash("Сцена не найдена.", "danger")
            return redirect(url_for("template4_detail", template_name=template_name))
        steps = scene.get("steps") if isinstance(scene.get("steps"), list) else []
        new_steps = [s for s in steps if str(s.get("id")) != str(step_id)]
        if len(new_steps) == len(steps):
            flash("Шаг не найден.", "warning")
            return redirect(url_for("template4_detail", template_name=template_name))
        scene["steps"] = new_steps
        scene["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_template4_scenes(template_dir, scenes)
        flash("Шаг удален.", "success")
        return redirect(url_for("template4_detail", template_name=template_name))

    @app.route("/templates4/<template_name>/scenes/<scene_id>/steps/<step_id>/move", methods=["POST"])
    def move_template4_scene_step(template_name, scene_id, step_id):
        template_dir = safe_template4_dir(template_name)
        if not template_dir:
            abort(404)
        direction = (request.form.get("direction") or "").strip().lower()
        if direction not in {"up", "down"}:
            flash("Некорректное направление перемещения.", "danger")
            return redirect(url_for("template4_detail", template_name=template_name))
        scenes, scene, _step = get_scene_and_step(template_dir, scene_id, step_id)
        if not scene:
            flash("Сцена не найдена.", "danger")
            return redirect(url_for("template4_detail", template_name=template_name))
        steps = scene.get("steps") if isinstance(scene.get("steps"), list) else []
        idx = next((i for i, s in enumerate(steps) if str(s.get("id")) == str(step_id)), -1)
        if idx < 0:
            flash("Шаг не найден.", "warning")
            return redirect(url_for("template4_detail", template_name=template_name))
        if direction == "up" and idx > 0:
            steps[idx - 1], steps[idx] = steps[idx], steps[idx - 1]
        elif direction == "down" and idx < len(steps) - 1:
            steps[idx + 1], steps[idx] = steps[idx], steps[idx + 1]
        scene["steps"] = steps
        scene["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_template4_scenes(template_dir, scenes)
        return redirect(url_for("template4_detail", template_name=template_name))

    @app.route("/templates4/<template_name>/generate", methods=["POST"])
    def generate_template4_scene(template_name):
        template_dir = safe_template4_dir(template_name)
        if not template_dir:
            abort(404)
        scene_id = (request.form.get("scene_id") or "").strip()
        if not scene_id:
            flash("Не передан идентификатор сцены.", "danger")
            return redirect(url_for("template4_detail", template_name=template_name))
        scenes = load_template4_scenes(template_dir)
        target = next((s for s in scenes if str(s.get("id")) == scene_id), None)
        if not target:
            flash("Сцена не найдена.", "danger")
            return redirect(url_for("template4_detail", template_name=template_name))
        target["status"] = "queued"
        target["message"] = "Задача добавлена в очередь"
        target["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_template4_scenes(template_dir, scenes)
        update_template_job(
            template_dir,
            scene_id,
            status="queued",
            message=target["message"],
            event_name="template4_job_update",
            room_prefix="template4",
        )
        try:
            enqueue_template4_job(template_name, scene_id)
        except Exception as exc:
            target["status"] = "failed"
            target["message"] = str(exc)
            target["updated_at"] = datetime.now(timezone.utc).isoformat()
            save_template4_scenes(template_dir, scenes)
            update_template_job(
                template_dir,
                scene_id,
                status="failed",
                message=str(exc),
                event_name="template4_job_update",
                room_prefix="template4",
            )
            flash(f"Не удалось поставить в очередь: {exc}", "danger")
            return redirect(url_for("template4_detail", template_name=template_name))
        flash("Генерация сцены запущена.", "success")
        return redirect(url_for("template4_detail", template_name=template_name))

    @app.route("/templates4/<template_name>/regenerate", methods=["POST"])
    def regenerate_template4_scene(template_name):
        template_dir = safe_template4_dir(template_name)
        if not template_dir:
            abort(404)
        scene_id = (request.form.get("scene_id") or "").strip()
        if not scene_id:
            flash("Не передан идентификатор сцены.", "danger")
            return redirect(url_for("template4_detail", template_name=template_name))
        scenes = load_template4_scenes(template_dir)
        target = next((s for s in scenes if str(s.get("id")) == scene_id), None)
        if not target:
            flash("Сцена не найдена.", "danger")
            return redirect(url_for("template4_detail", template_name=template_name))

        target["video_url"] = ""
        target["kling_task_id"] = ""
        target["status"] = "queued"
        target["message"] = "Перегенерация: задача добавлена в очередь"
        target["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_template4_scenes(template_dir, scenes)
        update_template_job(
            template_dir,
            scene_id,
            status="queued",
            message=target["message"],
            output_url="",
            event_name="template4_job_update",
            room_prefix="template4",
        )
        try:
            enqueue_template4_job(template_name, scene_id)
        except Exception as exc:
            target["status"] = "failed"
            target["message"] = str(exc)
            target["updated_at"] = datetime.now(timezone.utc).isoformat()
            save_template4_scenes(template_dir, scenes)
            update_template_job(
                template_dir,
                scene_id,
                status="failed",
                message=str(exc),
                event_name="template4_job_update",
                room_prefix="template4",
            )
            flash(f"Не удалось поставить в очередь: {exc}", "danger")
            return redirect(url_for("template4_detail", template_name=template_name))
        flash("Перегенерация сцены запущена.", "success")
        return redirect(url_for("template4_detail", template_name=template_name))

    @app.route("/api/templates4/<template_name>/scenes")
    def api_template4_scenes(template_name):
        template_dir = safe_template4_dir(template_name)
        if not template_dir:
            return jsonify({"ok": False, "error": "template not found"}), 404
        return jsonify({"ok": True, "scenes": load_template4_scenes(template_dir)})

    @app.route("/api/templates4/check-access")
    def api_templates4_check_access():
        model_name = (app.config.get("KLING_MODEL") or "").strip() or "kling-v2.6-std"
        has_jwt_keys = (app.config.get("KLING_ACCESS_KEY") or "").strip() and (app.config.get("KLING_SECRET_KEY") or "").strip()
        has_api_key = (app.config.get("KLING_API_KEY") or "").strip()
        if not has_jwt_keys and not has_api_key:
            return jsonify({"ok": False, "message": "Не заданы KLING_ACCESS_KEY/KLING_SECRET_KEY (или KLING_API_KEY)."}), 400
        try:
            base_url = (app.config.get("KLING_BASE_URL") or "").rstrip("/")
            fake_task_id = f"healthcheck-{uuid.uuid4().hex[:12]}"
            status_path_tpl = (app.config.get("KLING_TASK_STATUS_PATH") or "/v1/videos/{task_id}").strip()
            status_paths = [
                status_path_tpl.replace("{task_id}", fake_task_id),
                f"/v1/videos/{fake_task_id}",
                f"/v1/videos/image2video/{fake_task_id}",
            ]
            resp = None
            for p in status_paths:
                path = p if p.startswith("/") else f"/{p}"
                url = f"{base_url}{path}"
                candidate = kling_request("GET", url, json_body=None, timeout=20, allow_429_retry=False)
                if candidate.status_code == 404:
                    resp = candidate
                    break
                if candidate.status_code not in (404, 405):
                    resp = candidate
                    break
            if resp is None:
                return jsonify({"ok": False, "message": "Не удалось проверить доступ: неизвестный ответ API."}), 400
            if resp.status_code in (401, 403):
                return jsonify({"ok": False, "message": "Нет доступа: проверьте ключи KLING и права на модель."}), resp.status_code
            if resp.status_code == 429:
                retry_after = (resp.headers.get("Retry-After") or "").strip()
                wait_hint = f" Retry-After: {retry_after} сек." if retry_after else ""
                return jsonify({"ok": False, "message": f"KLING вернул 429 (лимит запросов).{wait_hint} Ключи валидны, но сейчас троттлинг."}), 429
            if resp.status_code in (200, 404):
                return jsonify({"ok": True, "message": f"Доступ к KLING API подтверждён. Модель по генерации: {model_name}."})
            if resp.status_code == 400:
                return jsonify({"ok": True, "message": f"Доступ к KLING API есть, модель: {model_name} (валидационный ответ 400 допустим)."})
            resp.raise_for_status()
            return jsonify({"ok": True, "message": f"Доступ к KLING API подтверждён. Модель: {model_name}."})
        except Exception as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400

    @app.route("/cartoons/create", methods=["GET", "POST"])
    def create_cartoon():
        if request.method == "GET":
            children = Child.query.order_by(Child.created_at.desc()).all()
            characters = Character.query.order_by(Character.created_at.desc()).all()
            return render_template("cartoon_create.html", children=children, characters=characters)

        story_prompt = request.form.get("story_prompt", "").strip()
        if not story_prompt:
            flash("Введите описание истории.", "danger")
            return redirect(url_for("create_cartoon"))

        if not app.config.get("OPENAI_API_KEY"):
            flash("OPENAI_API_KEY не задан в .env — генерация недоступна.", "danger")
            return redirect(url_for("create_cartoon"))

        child_ids = [int(x) for x in request.form.getlist("child_ids") if x.isdigit()]
        character_ids = [int(x) for x in request.form.getlist("character_ids") if x.isdigit()]

        cartoon = Cartoon(story_prompt=story_prompt, status="generating")
        db.session.add(cartoon)
        db.session.flush()

        for child_id in child_ids:
            db.session.add(CartoonParticipant(cartoon_id=cartoon.id, child_id=child_id))
        for char_id in character_ids:
            db.session.add(CartoonCharacterLink(cartoon_id=cartoon.id, character_id=char_id))

        db.session.commit()

        try:
            generate_storyboard(cartoon)
            db.session.commit()
            flash("История сгенерирована!", "success")
        except Exception as exc:
            app.logger.error("Storyboard generation failed cartoon=%d: %s", cartoon.id, exc)
            cartoon.status = "failed"
            db.session.commit()
            flash("Не удалось сгенерировать историю. Проверьте OPENAI_API_KEY и попробуйте снова.", "danger")

        return redirect(url_for("cartoon_detail", cartoon_id=cartoon.id))

    @app.route("/cartoons/<int:cartoon_id>")
    def cartoon_detail(cartoon_id):
        cartoon = Cartoon.query.get_or_404(cartoon_id)
        final_video_url = final_video_url_for_cartoon(cartoon_id)
        pipeline_status = get_pipeline_status(cartoon)
        pipeline_runtime = get_pipeline_runtime(cartoon_id)
        scene_start_images = {}
        scene_voice_audio = {}
        scene_sfx_audio = {}
        scene_mix_audio = {}
        for scene in cartoon.scenes:
            image_path = scene_asset_path(scene, "start.jpg")
            voice_path = scene_asset_path(scene, "voice.mp3")
            sfx_path = scene_asset_path(scene, "sfx.mp3")
            mix_path = scene_asset_path(scene, "mix.mp3")
            if os.path.exists(image_path):
                scene_start_images[scene.id] = scene_asset_url(scene, f"scene_{scene.scene_number:02d}_start.jpg")
            if os.path.exists(voice_path):
                scene_voice_audio[scene.id] = scene_asset_url(scene, f"scene_{scene.scene_number:02d}_voice.mp3")
            if os.path.exists(sfx_path):
                scene_sfx_audio[scene.id] = scene_asset_url(scene, f"scene_{scene.scene_number:02d}_sfx.mp3")
            if os.path.exists(mix_path):
                scene_mix_audio[scene.id] = scene_asset_url(scene, f"scene_{scene.scene_number:02d}_mix.mp3")
        return render_template(
            "cartoon_detail.html",
            cartoon=cartoon,
            final_video_url=final_video_url,
            pipeline_status=pipeline_status,
            pipeline_runtime=pipeline_runtime,
            scene_start_images=scene_start_images,
            scene_voice_audio=scene_voice_audio,
            scene_sfx_audio=scene_sfx_audio,
            scene_mix_audio=scene_mix_audio,
        )

    @app.route("/cartoons/<int:cartoon_id>/regenerate", methods=["POST"])
    def regenerate_cartoon(cartoon_id):
        cartoon = Cartoon.query.get_or_404(cartoon_id)
        if not app.config.get("OPENAI_API_KEY"):
            flash("OPENAI_API_KEY не задан в .env.", "danger")
            return redirect(url_for("cartoon_detail", cartoon_id=cartoon_id))

        for scene in list(cartoon.scenes):
            db.session.delete(scene)
        cartoon.status = "generating"
        db.session.commit()

        try:
            generate_storyboard(cartoon)
            db.session.commit()
            flash("История перегенерирована!", "success")
        except Exception as exc:
            app.logger.error("Regeneration failed cartoon=%d: %s", cartoon_id, exc)
            cartoon.status = "failed"
            db.session.commit()
            flash("Ошибка при генерации. Попробуйте ещё раз.", "danger")

        return redirect(url_for("cartoon_detail", cartoon_id=cartoon_id))

    @app.route("/cartoons/<int:cartoon_id>/edit", methods=["POST"])
    def edit_cartoon(cartoon_id):
        cartoon = Cartoon.query.get_or_404(cartoon_id)
        story_prompt = request.form.get("story_prompt", "").strip()
        if not story_prompt:
            flash("Идея истории не может быть пустой.", "danger")
            return redirect(url_for("cartoon_detail", cartoon_id=cartoon_id))
        cartoon.story_prompt = story_prompt
        db.session.commit()
        flash("Идея истории обновлена.", "success")
        return redirect(url_for("cartoon_detail", cartoon_id=cartoon_id))

    @app.route("/cartoons/<int:cartoon_id>/delete", methods=["POST"])
    def delete_cartoon(cartoon_id):
        cartoon = Cartoon.query.get_or_404(cartoon_id)
        db.session.delete(cartoon)
        db.session.commit()
        flash("Мультик удалён.", "success")
        return redirect(url_for("cartoons_list"))

    @app.route("/cartoons/<int:cartoon_id>/generate-video", methods=["POST"])
    def generate_cartoon_video(cartoon_id):
        cartoon = Cartoon.query.get_or_404(cartoon_id)
        ok, msg = start_pipeline_step_async(cartoon.id, "video", run_step_video)
        flash(msg, "info" if ok else "danger")
        return redirect(url_for("cartoon_detail", cartoon_id=cartoon_id))

    @app.route("/cartoons/<int:cartoon_id>/assemble", methods=["POST"])
    def assemble_cartoon(cartoon_id):
        cartoon = Cartoon.query.get_or_404(cartoon_id)
        ok, msg = start_pipeline_step_async(cartoon.id, "assemble", run_step_assemble)
        flash(msg, "info" if ok else "danger")
        return redirect(url_for("cartoon_detail", cartoon_id=cartoon_id))

    @app.route("/cartoons/<int:cartoon_id>/step/storyboard", methods=["POST"])
    def step_storyboard(cartoon_id):
        cartoon = Cartoon.query.get_or_404(cartoon_id)
        ok, msg = start_pipeline_step_async(cartoon.id, "storyboard", run_step_storyboard)
        flash(msg, "info" if ok else "danger")
        return redirect(url_for("cartoon_detail", cartoon_id=cartoon_id))

    @app.route("/cartoons/<int:cartoon_id>/step/images", methods=["POST"])
    def step_images(cartoon_id):
        cartoon = Cartoon.query.get_or_404(cartoon_id)
        ok, msg = start_pipeline_step_async(cartoon.id, "images", run_step_images)
        flash(msg, "info" if ok else "danger")
        return redirect(url_for("cartoon_detail", cartoon_id=cartoon_id))

    @app.route("/cartoons/<int:cartoon_id>/step/audio", methods=["POST"])
    def step_audio(cartoon_id):
        cartoon = Cartoon.query.get_or_404(cartoon_id)
        ok, msg = start_pipeline_step_async(cartoon.id, "audio", run_step_audio)
        flash(msg, "info" if ok else "danger")
        return redirect(url_for("cartoon_detail", cartoon_id=cartoon_id))

    @app.route("/cartoons/<int:cartoon_id>/step/video", methods=["POST"])
    def step_video(cartoon_id):
        cartoon = Cartoon.query.get_or_404(cartoon_id)
        ok, msg = start_pipeline_step_async(cartoon.id, "video", run_step_video)
        flash(msg, "info" if ok else "danger")
        return redirect(url_for("cartoon_detail", cartoon_id=cartoon_id))

    @app.route("/cartoons/<int:cartoon_id>/step/assemble", methods=["POST"])
    def step_assemble(cartoon_id):
        cartoon = Cartoon.query.get_or_404(cartoon_id)
        ok, msg = start_pipeline_step_async(cartoon.id, "assemble", run_step_assemble)
        flash(msg, "info" if ok else "danger")
        return redirect(url_for("cartoon_detail", cartoon_id=cartoon_id))

    @app.route("/api/cartoons/<int:cartoon_id>/pipeline-state")
    def api_pipeline_state(cartoon_id):
        runtime = get_pipeline_runtime(cartoon_id)
        snapshot = build_pipeline_snapshot(cartoon_id)
        return jsonify({**runtime, **snapshot})

    @app.route("/api/video-callback/<int:scene_id>", methods=["POST"])
    def api_video_callback(scene_id):
        raw = request.get_data(as_text=True)
        app.logger.info("VIDEO CALLBACK scene=%d body=%s", scene_id, raw)
        scene = CartoonScene.query.get_or_404(scene_id)
        data = request.get_json(silent=True) or {}
        inner = data.get("data", data)
        status = inner.get("status", "")
        urls = inner.get("output", {}).get("urls", [])
        changed = False
        if status == "completed" and urls:
            scene.video_status = "completed"
            scene.video_url = urls[0]
            changed = True
        elif status == "failed":
            scene.video_status = "failed"
            changed = True
        if changed:
            db.session.commit()
            socketio.emit("video_scene_ready", scene.to_dict(),
                          room=f"cartoon_{scene.cartoon_id}")
        return jsonify({"ok": True})

    @app.route("/api/cartoons/<int:cartoon_id>/video-sync")
    def api_video_sync(cartoon_id):
        """Recovery: re-fetch pending video tasks from API and emit socket events."""
        cartoon = Cartoon.query.get_or_404(cartoon_id)
        for scene in cartoon.scenes:
            if scene.video_status != "pending" or not scene.video_task_id:
                continue
            data = fetch_video_task_result(scene.video_task_id)
            if not data:
                continue
            inner = data.get("data", data)
            status = inner.get("status", "")
            urls = inner.get("output", {}).get("urls", [])
            changed = False
            if status == "completed" and urls:
                scene.video_status = "completed"
                scene.video_url = urls[0]
                changed = True
            elif status == "failed":
                scene.video_status = "failed"
                changed = True
            if changed:
                db.session.commit()
                socketio.emit("video_scene_ready", scene.to_dict(),
                              room=f"cartoon_{cartoon_id}")
        return jsonify({"ok": True})

    # ------------------------------------------------------------------ API

    @app.route("/api/callback/<int:avatar_id>", methods=["POST"])
    def api_callback(avatar_id):
        """
        Webhook called by aivideoapi when generation finishes.
        """
        raw_body = request.get_data(as_text=True)
        app.logger.info("CALLBACK avatar=%d body=%s", avatar_id, raw_body)

        avatar = CartoonAvatar.query.get_or_404(avatar_id)
        data = request.get_json(silent=True) or {}
        changed = apply_task_result(avatar, data)

        if changed:
            db.session.commit()
            socketio.emit("avatar_ready", avatar.to_dict(), room=f"child_{avatar.child_id}")

        return jsonify({"ok": True})

    @app.route("/api/children/<int:child_id>/sync")
    def api_sync(child_id):
        """
        Recovery endpoint: re-fetch status from aivideoapi for every pending
        avatar that has a task_id. Called automatically from the browser when
        the page has been open for a while without updates.
        """
        child = Child.query.get_or_404(child_id)
        updated = []

        for avatar in child.avatars:
            if avatar.status != "pending" or not avatar.task_id:
                continue
            if str(avatar.task_id).startswith("redis:"):
                continue
            data = fetch_avatar_task_result(avatar.task_id)
            if data and apply_task_result(avatar, data):
                db.session.commit()
                socketio.emit("avatar_ready", avatar.to_dict(), room=f"child_{child_id}")
                updated.append(avatar.id)
                app.logger.info("Recovery updated avatar=%d status=%s", avatar.id, avatar.status)

        return jsonify({"synced": updated})

    @app.route("/api/voice-preview", methods=["POST"])
    def api_voice_preview():
        voice_id = (request.form.get("voice_id") or "").strip()
        text = (request.form.get("text") or "").strip()
        if not text:
            text = "Привет. Это тест голоса для детского мультфильма."
        if not voice_id:
            return jsonify({"ok": False, "error": "voice_id is required"}), 400
        if not app.config.get("ELEVENLABS_API_KEY"):
            return jsonify({"ok": False, "error": "ELEVENLABS_API_KEY is not configured"}), 400

        audio_bytes, err = eleven_tts_bytes(text, voice_id=voice_id)
        if not audio_bytes:
            if err == "unauthorized":
                return jsonify({"ok": False, "error": "ElevenLabs unauthorized: проверьте ELEVENLABS_API_KEY и перезапустите приложение"}), 401
            if err == "voice_not_found":
                return jsonify({"ok": False, "error": "Voice ID не найден в вашем аккаунте ElevenLabs"}), 404
            if err == "missing_credentials":
                return jsonify({"ok": False, "error": "Не заданы ELEVENLABS_API_KEY или voice_id"}), 400
            return jsonify({"ok": False, "error": "Failed to generate preview audio"}), 502

        return Response(audio_bytes, mimetype="audio/mpeg")

    # --------------------------------------------------------- SocketIO events

    @socketio.on("watch_child")
    def on_watch_child(data):
        child_id = data.get("child_id")
        if child_id:
            join_room(f"child_{child_id}")

    @socketio.on("unwatch_child")
    def on_unwatch_child(data):
        child_id = data.get("child_id")
        if child_id:
            leave_room(f"child_{child_id}")

    @socketio.on("watch_cartoon")
    def on_watch_cartoon(data):
        cartoon_id = data.get("cartoon_id")
        if cartoon_id:
            join_room(f"cartoon_{cartoon_id}")

    @socketio.on("unwatch_cartoon")
    def on_unwatch_cartoon(data):
        cartoon_id = data.get("cartoon_id")
        if cartoon_id:
            leave_room(f"cartoon_{cartoon_id}")

    @socketio.on("watch_template")
    def on_watch_template(data):
        template_name = (data or {}).get("template_name")
        if template_name:
            join_room(f"template_{template_name}")

    @socketio.on("unwatch_template")
    def on_unwatch_template(data):
        template_name = (data or {}).get("template_name")
        if template_name:
            leave_room(f"template_{template_name}")

    @socketio.on("watch_template2")
    def on_watch_template2(data):
        template_name = (data or {}).get("template_name")
        if template_name:
            join_room(f"template2_{template_name}")

    @socketio.on("unwatch_template2")
    def on_unwatch_template2(data):
        template_name = (data or {}).get("template_name")
        if template_name:
            leave_room(f"template2_{template_name}")

    @socketio.on("watch_template3")
    def on_watch_template3(data):
        template_name = (data or {}).get("template_name")
        if template_name:
            join_room(f"template3_{template_name}")

    @socketio.on("unwatch_template3")
    def on_unwatch_template3(data):
        template_name = (data or {}).get("template_name")
        if template_name:
            leave_room(f"template3_{template_name}")

    @socketio.on("watch_template4")
    def on_watch_template4(data):
        template_name = (data or {}).get("template_name")
        if template_name:
            join_room(f"template4_{template_name}")
            template_dir = safe_template4_dir(str(template_name))
            scenes = load_template4_scenes(template_dir) if template_dir else []
            socketio.emit(
                "template4_scenes_snapshot",
                {
                    "template_name": template_name,
                    "scenes": scenes,
                },
                room=request.sid,
            )

    @socketio.on("unwatch_template4")
    def on_unwatch_template4(data):
        template_name = (data or {}).get("template_name")
        if template_name:
            leave_room(f"template4_{template_name}")

    @socketio.on("template4_run_step")
    def on_template4_run_step(data):
        payload = data or {}
        template_name = str(payload.get("template_name") or "").strip()
        scene_id = str(payload.get("scene_id") or "").strip()
        step_id = str(payload.get("step_id") or "").strip()
        if not template_name or not scene_id or not step_id:
            socketio.emit(
                "template4_action_result",
                {"ok": False, "message": "Не переданы template_name / scene_id / step_id."},
                room=request.sid,
            )
            return
        ok, message = queue_template4_step_run(template_name, scene_id, step_id)
        socketio.emit(
            "template4_action_result",
            {"ok": bool(ok), "message": message, "scene_id": scene_id, "step_id": step_id},
            room=request.sid,
        )

    ensure_template4_worker_started()
    resume_template4_processing_jobs()
    ensure_avatar_worker_started()

    return app


app = create_app()

if __name__ == "__main__":
    socketio.run(app, debug=True, host="0.0.0.0", port=5000, allow_unsafe_werkzeug=True)
