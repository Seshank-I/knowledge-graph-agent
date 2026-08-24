from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Central config. Nothing in the pipeline should read os.environ directly —
    always go through this, so every stage's dependencies are visible in one place.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Neo4j
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "testpassword123"

    # LLM
    # backend "api" uses the Anthropic SDK (needs anthropic_api_key);
    # "claude_cli" shells out to a locally-installed, already-authenticated
    # Claude Code CLI (`claude -p`) — useful when you have a subscription
    # but no API key. Same prompts, same validation, slower per call.
    llm_backend: str = "api"
    anthropic_api_key: str = ""
    llm_model: str = "claude-sonnet-4-5"

    # Target app / repo
    target_app_base_url: str = "https://app.cal.com"
    target_app_email: str = ""
    target_app_password: str = ""
    target_repo_path: str = "./data/cal.com"
    target_repo_slug: str = "calcom/cal.com"  # owner/name on GitHub, for PR fetch
    github_token: str = ""  # optional; raises GitHub API rate limits
    target_pr_number: int = 0

    # Confidence threshold below which an edge is flagged needs_review
    confidence_threshold: float = 0.5

    # Spec-parser scoping for tight live runs: cap the number of extracted
    # requirements (0 = unlimited) and/or restrict to feature areas
    # (comma-separated slugs, e.g. "booking,availability"; empty = all).
    spec_max_requirements: int = 0
    spec_feature_areas: str = ""


settings = Settings()
