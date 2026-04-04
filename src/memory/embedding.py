"""嵌入向量提供者 — Gemini primary, sentence-transformers fallback.

所有依赖均懒导入，缺少时静默降级，不影响事实库和中期摘要。
本地模型支持 device 配置：cuda / cpu / auto（默认 auto 即优先 GPU）。
"""
import logging
import os
from typing import List, Optional

logger = logging.getLogger(__name__)


def _torch_importable() -> bool:
    “””Quick check: can torch be imported right now?”””
    try:
        import torch  # noqa: F401
        return True
    except Exception:
        return False


def _ensure_pretrained_model_on_transformers() -> None:
    “””把 PreTrainedModel 挂到 transformers 包顶，兼容 sentence_transformers 的 `from transformers import PreTrainedModel`。

    切勿使用 ``hasattr(transformers, “PreTrainedModel”)``：在 transformers≥4.5 的 LazyModule 上
    这会触发与 ``from transformers import PreTrainedModel`` 相同的延迟加载；若子模块导入失败
    会抛出 ModuleNotFoundError（而非 AttributeError），导致整段补丁被 ``except: pass`` 吃掉，
    随后 sentence_transformers 仍报 “Could not import module 'PreTrainedModel'…”。

    正确做法：直接 ``import transformers.modeling_utils`` 再 setattr；失败时打日志便于排查
    （如打包版 user_packages 内残缺/版本混装的 transformers）。
    “””
    try:
        import transformers  # noqa: F401
        import transformers.modeling_utils as _mutils  # noqa: F401
        setattr(transformers, “PreTrainedModel”, _mutils.PreTrainedModel)
    except Exception as ex:
        # Diagnose the most common cause: torch absent or broken in user_packages.
        if not _torch_importable():
            logger.warning(
                “[embedding] PreTrainedModel 绑定失败：torch 不可用。”
                “sentence-transformers 需要 torch 才能加载本地 transformer 模型。”
                “请在「启动器 → Qwen3-TTS」或「设置 → 入门」中先安装 PyTorch，再重新测试 embedding。”
                “（原始错误: %s）”,
                ex,
            )
        else:
            logger.warning(
                “[embedding] PreTrainedModel 绑定失败（torch 存在但 transformers.modeling_utils 加载异常）: %s”,
                ex,
            )


def _resolve_embedding_device(config: dict) -> str:
    """解析 embedding 使用的设备：cuda / cpu / auto。"""
    raw = config.get("device") or "auto"
    device = (raw if isinstance(raw, str) else str(raw)).strip().lower()
    if device == "cpu":
        return "cpu"
    if device == "cuda" or device.startswith("cuda:"):
        return device
    # auto: 有 CUDA 则用 cuda，否则 cpu
    try:
        import torch  # type: ignore
        if torch.cuda.is_available():
            return "cuda"
    except ImportError:
        pass
    return "cpu"


class EmbeddingProvider:
    """统一 embedding 接口，优先 Gemini，失败时 fallback 本地模型。"""

    def __init__(self, config: dict):
        self._config = config
        self._provider = "none"
        self._api_key = ""
        self._local_model = None
        self._compat_client = None
        self._compat_model = ""
        self._init()

    def _init(self):
        provider = self._config.get("provider", "gemini")
        if provider == "gemini":
            self._try_init_gemini()
        elif provider == "openai_compat":
            self._try_init_openai_compat()
        elif provider == "local":
            self._try_init_local()
        if self._provider == "none":
            logger.info("[EmbeddingProvider] 无可用 embedding 后端，向量层已禁用")

    def _try_init_gemini(self):
        import os
        api_key = (os.environ.get("GEMINI_API_KEY")
                   or os.environ.get("GOOGLE_API_KEY")
                   or os.environ.get("EMBEDDING_API_KEY")
                   or self._config.get("api_key", "")
                   or self._config.get("gemini_api_key", ""))
        if not api_key:
            logger.warning("[EmbeddingProvider] 未找到 Gemini API key，尝试本地模型")
            self._try_init_local()
            return

        self._api_key = api_key
        self._gemini_model = (self._config.get("model") or self._config.get("gemini_model") or "gemini-embedding-001")

        # 启动时探测一次，确认 embedding 模型确实可用；失败则降级本地模型
        logger.info("[EmbeddingProvider] 探测 Gemini embedding model=%s ...", self._gemini_model)
        probe = self._embed_gemini(["test"])
        if probe is None:
            logger.warning(
                "[EmbeddingProvider] Gemini embedding 不可用 (model=%s)，切换本地模型",
                self._gemini_model,
            )
            self._try_init_local()
            return

        self._provider = "gemini"
        logger.info("[EmbeddingProvider] Gemini embedding 已就绪 (model=%s)", self._gemini_model)

    def _try_init_local(self):
        import os
        model_name = self._config.get("local_model", "all-MiniLM-L6-v2")
        device = _resolve_embedding_device(self._config)
        strict_offline = self._config.get("offline") is True  # 用户显式要求仅离线、不联网

        HF_MIRROR = "https://hf-mirror.com"
        # 在 import sentence_transformers 之前就设好镜像，否则库可能在首次请求时仍用默认 huggingface.co
        if not strict_offline and os.environ.get("HF_TRY_OFFICIAL_FIRST") != "1":
            os.environ.setdefault("HF_ENDPOINT", HF_MIRROR)

        os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
        os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
        os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "8")
        os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "8")
        for name in ("httpx", "httpcore", "sentence_transformers", "transformers",
                     "huggingface_hub", "urllib3", "urllib3.connectionpool"):
            logging.getLogger(name).setLevel(logging.WARNING)
        _ensure_pretrained_model_on_transformers()
        from sentence_transformers import SentenceTransformer  # type: ignore

        def _load_st(model_path_or_name: str, dev: str, local_files_only: bool = False):
            """加载模型；local_files_only=True 则完全跳过网络检查（有缓存时秒加载）。"""
            kwargs: dict = {}
            if local_files_only:
                kwargs["local_files_only"] = True
            try:
                return SentenceTransformer(model_path_or_name, device=dev, **kwargs)
            except Exception as e:
                err_lower = str(e).lower()
                if dev != "cpu" and ("cuda" in err_lower or "not compiled" in err_lower):
                    logger.info("[EmbeddingProvider] CUDA 不可用，改用 CPU: %s", e)
                    return SentenceTransformer(model_path_or_name, device="cpu", **kwargs)
                raise

        # 若是本地目录路径（如 models/all-MiniLM-L6-v2 或绝对路径），直接加载，不连 Hugging Face
        # 先按绝对路径尝试，再按相对于项目根拼接（兼容 PyInstaller exe 工作目录不固定的情况）
        raw_name = model_name.strip() if isinstance(model_name, str) else ""
        path = ""
        if raw_name:
            abs_candidate = os.path.abspath(raw_name)
            if os.path.isdir(abs_candidate):
                path = abs_candidate
            else:
                from src.utils.paths import get_project_root as _gpr
                root_candidate = os.path.join(_gpr(), raw_name)
                if os.path.isdir(root_candidate):
                    path = root_candidate
        if path and os.path.isdir(path):
            try:
                self._local_model = _load_st(path, device)
                self._provider = "local"
                logger.info("[EmbeddingProvider] 本地 sentence-transformers 已初始化 (path=%s device=%s)", path, getattr(self._local_model, "device", device))
                return
            except Exception as e:
                logger.warning("[EmbeddingProvider] 从本地路径加载失败 %s: %s", path, e)
            return

        # 否则按「模型名」从缓存或 Hugging Face 加载：先试离线，再试联网（仅当未勾选仅离线时）
        # HF_ENDPOINT 已在上方提前设为镜像（除非 HF_TRY_OFFICIAL_FIRST=1）
        def _is_network_error(exc: Exception) -> bool:
            s = str(exc).lower()
            return (
                "timeout" in s or "timed out" in s
                or "connection" in s
                or "max retries" in s
                or "connectionrefused" in s
                or "connecttimeout" in s
            )

        for attempt_offline in (True, False):
            if attempt_offline:
                os.environ["HF_HUB_OFFLINE"] = "1"
                os.environ["TRANSFORMERS_OFFLINE"] = "1"
            else:
                if strict_offline:
                    break
                os.environ.pop("HF_HUB_OFFLINE", None)
                os.environ.pop("TRANSFORMERS_OFFLINE", None)
                if os.environ.get("HF_ENDPOINT") == HF_MIRROR:
                    logger.info("[EmbeddingProvider] 使用国内镜像加载: %s", HF_MIRROR)
                logger.info("[EmbeddingProvider] 本地缓存未命中，尝试联网加载模型: %s", model_name)
            try:
                self._local_model = _load_st(model_name, device, local_files_only=attempt_offline)
                self._provider = "local"
                logger.info(
                    "[EmbeddingProvider] 本地 sentence-transformers 已初始化 (model=%s device=%s%s)",
                    model_name, getattr(self._local_model, "device", device), " 离线" if attempt_offline else " 联网",
                )
                if attempt_offline:
                    os.environ.pop("HF_HUB_OFFLINE", None)
                    os.environ.pop("TRANSFORMERS_OFFLINE", None)
                return
            except Exception as e:
                if attempt_offline and not strict_offline:
                    logger.info("[EmbeddingProvider] 离线加载失败 (%s)，将尝试联网", e)
                    continue
                # 联网阶段失败：若为网络错误且尚未用镜像，则换镜像重试一次（仅当 HF_TRY_OFFICIAL_FIRST=1 时才会走到这里）
                if not attempt_offline and _is_network_error(e) and os.environ.get("HF_ENDPOINT") != HF_MIRROR:
                    old_endpoint = os.environ.pop("HF_ENDPOINT", None)
                    os.environ["HF_ENDPOINT"] = HF_MIRROR
                    logger.info("[EmbeddingProvider] 直连 Hugging Face 失败，尝试国内镜像: %s", HF_MIRROR)
                    try:
                        self._local_model = _load_st(model_name, device)
                        self._provider = "local"
                        logger.info(
                            "[EmbeddingProvider] 本地 sentence-transformers 已初始化 (model=%s device=%s 镜像)",
                            model_name, getattr(self._local_model, "device", device),
                        )
                        return
                    except Exception as e2:
                        logger.warning("[EmbeddingProvider] 镜像加载也失败: %s", e2)
                    finally:
                        if old_endpoint is not None:
                            os.environ["HF_ENDPOINT"] = old_endpoint
                        else:
                            os.environ.pop("HF_ENDPOINT", None)
                logger.warning("[EmbeddingProvider] 本地模型初始化失败: %s", e)
                break

    def _try_init_openai_compat(self):
        base_url = (self._config.get("base_url") or "").strip()
        api_key = (self._config.get("api_key") or "").strip() or os.environ.get("EMBEDDING_API_KEY", "")
        model = (self._config.get("model") or self._config.get("gemini_model") or "text-embedding-3-small").strip()
        if not base_url:
            logger.warning("[EmbeddingProvider] openai_compat: base_url 未配置，embedding 禁用")
            return
        from openai import OpenAI
        self._compat_client = OpenAI(
            api_key=api_key or "sk-placeholder",
            base_url=base_url,
            timeout=30.0,
        )
        self._compat_model = model
        logger.info("[EmbeddingProvider] 探测 OpenAI-compat embedding base_url=%s model=%s ...", base_url, model)
        probe = self._embed_compat(["test"])
        if probe is None:
            logger.warning("[EmbeddingProvider] openai_compat probe 失败，embedding 禁用")
            return
        self._provider = "openai_compat"
        logger.info("[EmbeddingProvider] OpenAI-compat embedding 就绪 (base_url=%s model=%s)", base_url, model)

    def embed(self, texts: List[str]) -> Optional[List[List[float]]]:
        """将文本列表转为向量，失败返回 None。"""
        if not texts:
            return []
        if self._provider == "gemini":
            return self._embed_gemini(texts)
        if self._provider == "openai_compat":
            return self._embed_compat(texts)
        if self._provider == "local":
            return self._embed_local(texts)
        return None

    def _embed_gemini(self, texts: List[str]) -> Optional[List[List[float]]]:
        """通过 openai SDK 调用 Gemini OpenAI-compat embeddings 端点。
        openai SDK 内部使用 httpx，会正确遵守系统代理设置（与 LLM 调用路径一致）。
        带 30 秒超时，避免网络不可达时一直卡住。
        """
        try:
            from openai import OpenAI
            base_url = self._config.get(
                "gemini_base_url",
                "https://generativelanguage.googleapis.com/v1beta/openai/",
            )
            client = OpenAI(
                api_key=self._api_key,
                base_url=base_url,
                timeout=30.0,
            )
            resp = client.embeddings.create(input=texts, model=self._gemini_model)
            return [item.embedding for item in resp.data]
        except Exception as e:
            logger.warning("[EmbeddingProvider] Gemini embed 失败: %s", e)
            return None

    def _embed_compat(self, texts: List[str]) -> Optional[List[List[float]]]:
        try:
            resp = self._compat_client.embeddings.create(input=texts, model=self._compat_model)
            return [item.embedding for item in resp.data]
        except Exception as e:
            logger.warning("[EmbeddingProvider] openai_compat embed 失败: %s", e)
            return None

    def _embed_local(self, texts: List[str]) -> Optional[List[List[float]]]:
        try:
            vecs = self._local_model.encode(texts, convert_to_numpy=True)
            return [v.tolist() for v in vecs]
        except Exception as e:
            logger.error("[EmbeddingProvider] 本地 embed 失败: %s", e)
            return None

    def is_available(self) -> bool:
        return self._provider != "none"

    def provider_name(self) -> str:
        return self._provider


def make_chroma_embedding_function(config: dict):
    """返回 ChromaDB 兼容的 EmbeddingFunction，不可用时返回 None。"""
    try:
        import chromadb  # type: ignore  # noqa
        from chromadb import EmbeddingFunction, Documents, Embeddings  # type: ignore

        provider = EmbeddingProvider(config)
        if not provider.is_available():
            return None

        class _ShikiEmbeddingFn(EmbeddingFunction):
            def __call__(self, input: Documents) -> Embeddings:  # type: ignore
                vecs = provider.embed(list(input))
                if vecs is None:
                    raise RuntimeError("EmbeddingProvider 返回 None")
                return vecs

        fn = _ShikiEmbeddingFn()
        fn._actual_provider = provider.provider_name()  # type: ignore[attr-defined]
        return fn
    except ImportError:
        return None
    except Exception as e:
        logger.error("[make_chroma_embedding_function] 失败: %s", e)
        return None
