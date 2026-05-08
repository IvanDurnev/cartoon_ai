import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")

    POSTGRES_USER = os.environ.get("POSTGRES_USER", "postgres")
    POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "")
    POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "127.0.0.1")
    POSTGRES_PORT = os.environ.get("POSTGRES_PORT", "5432")
    POSTGRES_DB = os.environ.get("POSTGRES_DB", "cartoons_ai")

    SQLALCHEMY_DATABASE_URI = (
        f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}"
        f"@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    AIVIDEOAPI_KEY = os.environ.get("AIVIDEOAPI_KEY", "")
    AIVIDEOAPI_BASE_URL = "https://api.aivideoapi.ai"
    AIVIDEO_IMAGE_MODEL = os.environ.get("AIVIDEO_IMAGE_MODEL", "gpt-image-1.5")
    AIVIDEO_VIDEO_MODEL = os.environ.get("AIVIDEO_VIDEO_MODEL", "ray-3.14")
    PIXVERSE_AI_API_KEY = os.environ.get("PIXVERSE_AI_API_KEY", "")
    PIXVERSE_BASE_URL = os.environ.get("PIXVERSE_BASE_URL", "https://app-api.pixverse.ai")
    PIXVERSE_QUALITY = os.environ.get("PIXVERSE_QUALITY", "540p")
    PIXVERSE_POLL_INTERVAL_SECONDS = int(os.environ.get("PIXVERSE_POLL_INTERVAL_SECONDS", "5"))
    PIXVERSE_POLL_MAX_ATTEMPTS = int(os.environ.get("PIXVERSE_POLL_MAX_ATTEMPTS", "60"))
    REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    PIXVERSE_QUEUE_NAME = os.environ.get("PIXVERSE_QUEUE_NAME", "pixverse:video:jobs")
    AVATAR_QUEUE_NAME = os.environ.get("AVATAR_QUEUE_NAME", "avatar:generate:jobs")

    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
    OPENAI_STORYBOARD_MODEL = os.environ.get("OPENAI_STORYBOARD_MODEL", "gpt-4o")
    OPENAI_IMAGE_MODEL = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-1")
    OPENAI_AVATAR_MODEL = os.environ.get("OPENAI_AVATAR_MODEL", "gpt-image-2")
    OPENAI_AVATAR_SIZE = os.environ.get("OPENAI_AVATAR_SIZE", "1024x1024")

    ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
    ELEVENLABS_BASE_URL = os.environ.get("ELEVENLABS_BASE_URL", "https://api.elevenlabs.io")
    ELEVENLABS_CREATIVE_BASE_URL = os.environ.get("ELEVENLABS_CREATIVE_BASE_URL", "https://api.elevenlabs.io")
    ELEVENLABS_CREATIVE_GENERATE_PATH = os.environ.get("ELEVENLABS_CREATIVE_GENERATE_PATH", "/v1/creative/image-video")
    ELEVENLABS_CREATIVE_STATUS_PATH = os.environ.get("ELEVENLABS_CREATIVE_STATUS_PATH", "/v1/creative/image-video/{generation_id}")
    ELEVENLABS_CREATIVE_MODEL_ID = os.environ.get("ELEVENLABS_CREATIVE_MODEL_ID", "")
    ELEVENLABS_CREATIVE_QUEUE_NAME = os.environ.get("ELEVENLABS_CREATIVE_QUEUE_NAME", "elevencreative:video:jobs")
    ELEVENLABS_CREATIVE_POLL_INTERVAL_SECONDS = int(os.environ.get("ELEVENLABS_CREATIVE_POLL_INTERVAL_SECONDS", "5"))
    ELEVENLABS_CREATIVE_POLL_MAX_ATTEMPTS = int(os.environ.get("ELEVENLABS_CREATIVE_POLL_MAX_ATTEMPTS", "60"))
    ELEVENLABS_VOICE_MODEL = os.environ.get("ELEVENLABS_VOICE_MODEL", "eleven_v3")
    ELEVENLABS_MUSIC_MODEL = os.environ.get("ELEVENLABS_MUSIC_MODEL", "music_v1")
    ELEVENLABS_SFX_MODEL = os.environ.get("ELEVENLABS_SFX_MODEL", "eleven_text_to_sound_v2")
    ELEVENLABS_SFX_DURATION_SECONDS = float(os.environ.get("ELEVENLABS_SFX_DURATION_SECONDS", "6"))
    ELEVENLABS_SFX_PROMPT_MAX_CHARS = int(os.environ.get("ELEVENLABS_SFX_PROMPT_MAX_CHARS", "220"))
    ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "")
    NARRATOR_DEFAULT_VOICE_ID = os.environ.get("NARRATOR_DEFAULT_VOICE_ID", "TX3LPaxmHKxFdv7VOQHJ")
    SD_WEBUI_BASE_URL = os.environ.get("SD_WEBUI_BASE_URL", "http://127.0.0.1:7860")
    SD_WEBUI_API_KEY = os.environ.get("SD_WEBUI_API_KEY", "")
    SD_VIDEO_QUEUE_NAME = os.environ.get("SD_VIDEO_QUEUE_NAME", "sdvideo:jobs")
    TEMPLATE4_QUEUE_NAME = os.environ.get("TEMPLATE4_QUEUE_NAME", "template4:video:jobs")
    TEMPLATE4_MAX_VIDEO_SECONDS = int(os.environ.get("TEMPLATE4_MAX_VIDEO_SECONDS", "10"))
    TEMPLATE4_POLL_INTERVAL_SECONDS = int(os.environ.get("TEMPLATE4_POLL_INTERVAL_SECONDS", "5"))
    TEMPLATE4_POLL_MAX_ATTEMPTS = int(os.environ.get("TEMPLATE4_POLL_MAX_ATTEMPTS", "60"))
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
    GEMINI_API_BASE_URL = os.environ.get("GEMINI_API_BASE_URL", "https://generativelanguage.googleapis.com/v1beta")
    TEMPLATE4_VEO_MODEL = os.environ.get("TEMPLATE4_VEO_MODEL", "veo-3.1-generate-preview")
    KLING_ACCESS_KEY = os.environ.get("KLING_ACCESS_KEY", "")
    KLING_SECRET_KEY = os.environ.get("KLING_SECRET_KEY", "")
    KLING_API_KEY = os.environ.get("KLING_API_KEY", "")
    KLING_BASE_URL = os.environ.get("KLING_BASE_URL", "https://api.klingai.com")
    KLING_IMAGE2VIDEO_PATH = os.environ.get("KLING_IMAGE2VIDEO_PATH", "/v1/videos/image2video")
    KLING_TASK_STATUS_PATH = os.environ.get("KLING_TASK_STATUS_PATH", "/v1/videos/{task_id}")
    KLING_MODEL = os.environ.get("KLING_MODEL", "kling-v2.6-std")
    KLING_MODE = os.environ.get("KLING_MODE", "standard")
    KLING_ASPECT_RATIO = os.environ.get("KLING_ASPECT_RATIO", "16:9")
    KLING_TOKEN_TTL_SECONDS = int(os.environ.get("KLING_TOKEN_TTL_SECONDS", "1800"))
    KLING_ENABLE_AUDIO = os.environ.get("KLING_ENABLE_AUDIO", "true").lower() in {"1", "true", "yes", "on"}
    KLING_AVATAR_STRATEGY = os.environ.get("KLING_AVATAR_STRATEGY", "image_tail")
    KLING_IMAGE_ENCODING = os.environ.get("KLING_IMAGE_ENCODING", "url")  # url | base64
    TEMPLATE4_SUBMIT_MAX_RETRIES = int(os.environ.get("TEMPLATE4_SUBMIT_MAX_RETRIES", "0"))
    TEMPLATE4_SUBMIT_RETRY_BASE_SECONDS = int(os.environ.get("TEMPLATE4_SUBMIT_RETRY_BASE_SECONDS", "12"))
    SD_MODEL_CHECKPOINT = os.environ.get("SD_MODEL_CHECKPOINT", "")
    SD_SAMPLER_NAME = os.environ.get("SD_SAMPLER_NAME", "DPM++ 2M Karras")
    SD_STEPS = int(os.environ.get("SD_STEPS", "24"))
    SD_CFG_SCALE = float(os.environ.get("SD_CFG_SCALE", "6.0"))
    SD_DENOISE_STRENGTH = float(os.environ.get("SD_DENOISE_STRENGTH", "0.55"))
    SD_CONTROLNET_MODULE = os.environ.get("SD_CONTROLNET_MODULE", "none")
    SD_CONTROLNET_MODEL = os.environ.get("SD_CONTROLNET_MODEL", "")
    SD_CONTROLNET_WEIGHT = float(os.environ.get("SD_CONTROLNET_WEIGHT", "0.9"))
    SD_FACE_SCRIPT_NAME = os.environ.get("SD_FACE_SCRIPT_NAME", "reactor")
    SD_CONNECT_TIMEOUT_SECONDS = int(os.environ.get("SD_CONNECT_TIMEOUT_SECONDS", "30"))
    # 0 means "no read timeout" (recommended for long-running background frame jobs)
    SD_READ_TIMEOUT_SECONDS = int(os.environ.get("SD_READ_TIMEOUT_SECONDS", "0"))
    SD_FRAME_RETRIES = int(os.environ.get("SD_FRAME_RETRIES", "2"))
    ELEVENLABS_PRESET_VOICES = [
        {"id": "EXAVITQu4vr4xnSDxMaL", "name": "Bella"},
        {"id": "21m00Tcm4TlvDq8ikWAM", "name": "Rachel"},
        {"id": "AZnzlk1XvdvUeBnXmlld", "name": "Domi"},
        {"id": "ErXwobaYiN019PkySvjV", "name": "Antoni"},
        {"id": "MF3mGyEYCl7XYWbV9V6O", "name": "Elli"},
        {"id": "TxGEqnHWrfWFTfGW9XjX", "name": "Josh"},
        {"id": "VR6AewLTigWG4xSOukaG", "name": "Arnold"},
        {"id": "pNInz6obpgDQGcFmaJgB", "name": "Adam"},
        {"id": "yoZ06aMxZJJ28mfd3POQ", "name": "Sam"},
        {"id": "piTKgcLEGmPE4e6mEKli", "name": "Nicole"},
        {"id": "N2lVS1w4EtoT3dr4eOWO", "name": "Callum"},
        {"id": "ODq5zmih8GrVes37Dizd", "name": "Patrick"},
        {"id": "SOYHLrjzK2X1ezoPC6cr", "name": "Harry"},
        {"id": "TX3LPaxmHKxFdv7VOQHJ", "name": "Liam"},
        {"id": "ThT5KcBeYPX3keUQqHPh", "name": "Dorothy"},
    ]

    # Public server URL — used both for serving uploaded photos (image_urls)
    # and for building the callback_url sent to aivideoapi.
    SERVER = os.environ.get("SERVER", "").rstrip("/")

    UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "static", "uploads")
    MAX_CONTENT_LENGTH = 10 * 1024 * 1024  # 10 MB
    ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
    GENERATED_FOLDER = os.path.join(os.path.dirname(__file__), "static", "generated")

    # 3 cartoon styles sent to the API
    CARTOON_STYLES = [
        {
            "name": "Disney",
            "prompt_suffix": (
                "Transform this child's photo into a Disney animated movie character: "
                "soft round face, big expressive eyes, vibrant warm colors, friendly smile, "
                "clean cartoon illustration style, high quality."
            ),
        },
        {
            "name": "Pixar",
            "prompt_suffix": (
                "Transform this child's photo into a Pixar 3D animation style character: "
                "cute proportions, expressive eyes, smooth colorful render, "
                "cheerful expression, studio-quality 3D cartoon look."
            ),
        },
        {
            "name": "Аниме",
            "prompt_suffix": (
                "Transform this child's photo into a Japanese anime style character: "
                "large shiny eyes, pastel color palette, cute chibi proportions, "
                "bright background, high-quality anime illustration."
            ),
        },
    ]
