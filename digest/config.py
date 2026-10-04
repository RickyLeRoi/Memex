from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


class ConfigError(Exception):
    pass


@dataclass
class LLMConfig:
    base_url: str = "http://localhost:11434/v1"
    model: str = "qwen2.5:14b"
    api_key: str = "local"
    temperature: float = 0.1
    max_tokens: int = 4096
    timeout: float = 600.0
    json_mode: str = "json_object"
    chunk_chars: int = 12000
    vision_model: str = ""
    extra_body: dict = field(default_factory=dict)


@dataclass
class ExtractConfig:
    me: str = ""
    language: str = "italiano"
    focus: str = ""
    final_summary: bool = True


@dataclass
class GraphConfig:
    client_id: str = ""
    tenant: str = "organizations"


@dataclass
class MailConfig:
    enabled: bool = False
    folders: list[str] = field(default_factory=lambda: ["inbox"])
    max_messages: int = 200
    max_body_chars: int = 6000
    strip_quoted: bool = True
    exclude_senders: list[str] = field(
        default_factory=lambda: ["noreply", "no-reply", "donotreply", "newsletter", "notifications@"]
    )


@dataclass
class TeamsConfig:
    enabled: bool = False
    max_chats: int = 50
    max_messages_per_chat: int = 200
    context_messages: int = 5
    channels: bool = False
    channel_allowlist: list[str] = field(default_factory=list)
    max_messages_per_channel: int = 100


@dataclass
class NotionConfig:
    enabled: bool = False
    token: str = ""
    max_pages: int = 50
    max_depth: int = 2
    max_page_chars: int = 15000


@dataclass
class SlackConfig:
    enabled: bool = False
    token: str = ""  # xoxp-... (user token) or xoxb-... (bot token, must be invited to the channels)
    channels: list[str] = field(default_factory=list)
    ticket_channels: list[str] = field(default_factory=list)
    max_messages_per_channel: int = 200
    include_threads: bool = True

    def is_configured(self) -> bool:
        return bool(self.token)


@dataclass
class GmailConfig:
    enabled: bool = False
    user: str = ""
    app_password: str = ""  # Google account app password (IMAP), never the real password
    folders: list[str] = field(default_factory=lambda: ["INBOX"])
    max_messages: int = 200
    max_body_chars: int = 6000
    strip_quoted: bool = True
    exclude_senders: list[str] = field(
        default_factory=lambda: ["noreply", "no-reply", "donotreply", "newsletter", "notifications@"]
    )
    host: str = "imap.gmail.com"

    def is_configured(self) -> bool:
        return bool(self.user and self.app_password)


@dataclass
class JiraConfig:
    enabled: bool = False
    base_url: str = ""
    email: str = ""
    api_token: str = ""
    projects: list[str] = field(default_factory=list)
    jql: str = ""
    max_issues: int = 100
    max_comments: int = 15
    max_description_chars: int = 4000

    def is_configured(self) -> bool:
        return bool(self.base_url and self.email and self.api_token)


@dataclass
class ServerConfig:
    allowed_hosts: list[str] = field(default_factory=list)
    token: str = ""  # when set, the GUI asks for it (HTTP Basic: any user name, the token as password)


@dataclass
class LinksConfig:
    enabled: bool = True
    file: str = "links.txt"
    use_ytdlp: bool = True
    cookies_from_browser: str = ""
    cookies_file: str = ""
    use_playwright: bool = False
    transcribe: bool = False
    whisper_model: str = "small"
    max_attempts: int = 3
    max_chars: int = 15000
    fetch_images: bool = True
    max_image_bytes: int = 5_000_000
    max_screenshot_bytes: int = 10_000_000
    max_pdf_bytes: int = 50_000_000
    max_pdf_pages: int = 15  # pages read per PDF; the vision model is slow on scanned pages


@dataclass
class ObsidianConfig:
    enabled: bool = False
    vault: str = ""
    folder: str = "Digest"
    links_folder: str = "Digest/Link"
    tasks_format: bool = True
    copy_media: bool = True
    copy_documents: bool = True


@dataclass
class Config:
    data_dir: str = "~/.memex"
    reports_dir: str = "reports"
    initial_lookback_days: int = 3
    llm: LLMConfig = field(default_factory=LLMConfig)
    extract: ExtractConfig = field(default_factory=ExtractConfig)
    graph: GraphConfig = field(default_factory=GraphConfig)
    mail: MailConfig = field(default_factory=MailConfig)
    teams: TeamsConfig = field(default_factory=TeamsConfig)
    notion: NotionConfig = field(default_factory=NotionConfig)
    slack: SlackConfig = field(default_factory=SlackConfig)
    gmail: GmailConfig = field(default_factory=GmailConfig)
    jira: JiraConfig = field(default_factory=JiraConfig)
    links: LinksConfig = field(default_factory=LinksConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    obsidian: ObsidianConfig = field(default_factory=ObsidianConfig)
    base_dir: Path = field(default_factory=Path.cwd)

    @property
    def data_path(self) -> Path:
        p = Path(os.path.expanduser(self.data_dir))
        if not p.is_absolute():
            p = self.base_dir / p
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def reports_path(self) -> Path:
        p = Path(os.path.expanduser(self.reports_dir))
        if not p.is_absolute():
            p = self.base_dir / p
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def links_path(self) -> Path:
        p = Path(os.path.expanduser(self.links.file))
        return p if p.is_absolute() else self.base_dir / p


SECTIONS = {
    "llm": LLMConfig,
    "extract": ExtractConfig,
    "graph": GraphConfig,
    "mail": MailConfig,
    "teams": TeamsConfig,
    "notion": NotionConfig,
    "slack": SlackConfig,
    "gmail": GmailConfig,
    "jira": JiraConfig,
    "links": LinksConfig,
    "server": ServerConfig,
    "obsidian": ObsidianConfig,
}


def _build(cls, data: dict, where: str):
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"[{where}] chiavi sconosciute: {', '.join(sorted(unknown))}")
    return cls(**data)


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Config non trovata: {path}. Lancia `python -m digest init` per crearne una.")
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    kwargs: dict = {}
    for key, value in raw.items():
        if key in SECTIONS:
            if not isinstance(value, dict):
                raise ConfigError(f"[{key}] deve essere una sezione")
            kwargs[key] = _build(SECTIONS[key], value, key)
        else:
            kwargs[key] = value
    kwargs["base_dir"] = path.resolve().parent
    cfg = _build(Config, kwargs, "root")

    # Secrets and overrides come from the environment so they never land in the file
    env = os.environ
    cfg.notion.token = env.get("NOTION_TOKEN", cfg.notion.token)
    cfg.slack.token = env.get("SLACK_TOKEN", cfg.slack.token)
    cfg.gmail.user = env.get("GMAIL_USER", cfg.gmail.user)
    cfg.gmail.app_password = env.get("GMAIL_APP_PASSWORD", cfg.gmail.app_password)
    cfg.jira.base_url = env.get("JIRA_BASE_URL", cfg.jira.base_url).rstrip("/")
    cfg.jira.email = env.get("JIRA_EMAIL", cfg.jira.email)
    cfg.jira.api_token = env.get("JIRA_API_TOKEN", cfg.jira.api_token)
    cfg.server.token = env.get("DIGEST_GUI_TOKEN", cfg.server.token)
    cfg.server.allowed_hosts += [h.strip() for h in env.get("DIGEST_ALLOWED_HOSTS", "").split(",") if h.strip()]
    cfg.llm.api_key = env.get("DIGEST_LLM_API_KEY", cfg.llm.api_key)
    cfg.llm.base_url = env.get("DIGEST_LLM_BASE_URL", cfg.llm.base_url).rstrip("/")
    cfg.llm.model = env.get("DIGEST_LLM_MODEL", cfg.llm.model)
    cfg.graph.client_id = env.get("DIGEST_GRAPH_CLIENT_ID", cfg.graph.client_id)
    cfg.graph.tenant = env.get("DIGEST_GRAPH_TENANT", cfg.graph.tenant)

    if cfg.llm.json_mode not in ("json_object", "none"):
        raise ConfigError("[llm] json_mode deve essere 'json_object' o 'none'")
    if (cfg.mail.enabled or cfg.teams.enabled) and not cfg.graph.client_id:
        raise ConfigError("[graph] client_id mancante: serve per Outlook e Teams (vedi README)")
    if cfg.notion.enabled and not cfg.notion.token:
        raise ConfigError("[notion] token mancante (o variabile NOTION_TOKEN)")
    for name, hint in (("slack", "token (o SLACK_TOKEN)"), ("gmail", "user e app_password (o GMAIL_USER/GMAIL_APP_PASSWORD)"),
                       ("jira", "base_url, email e api_token (o JIRA_*)")):
        section = getattr(cfg, name)
        if section.enabled and not section.is_configured():
            raise ConfigError(f"[{name}] configurazione incompleta: servono {hint}")
    if cfg.obsidian.enabled:
        vault = Path(os.path.expanduser(cfg.obsidian.vault)) if cfg.obsidian.vault else None
        if not vault or not vault.is_dir():
            raise ConfigError(f"[obsidian] vault non trovato: {cfg.obsidian.vault!r}")
    return cfg
