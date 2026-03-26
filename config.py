from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # MiMo API
    mimo_api_key: str
    mimo_api_base: str = "https://api.xiaomimimo.com/v1"
    mimo_model: str = "mimo-v2-pro"
    mimo_max_tokens: int = 1024
    mimo_temperature: float = 1.0
    mimo_top_p: float = 0.95

    # GLM 备用文本模型（可选）
    glm_api_key: str = ""
    glm_api_base: str = "https://open.bigmodel.cn/api/paas/v4"
    glm_model: str = "glm-4.7-flash"

    # DeepSeek 文本模型（可选）
    deepseek_api_key: str = ""
    deepseek_api_base: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"

    # 文本模型默认路由：auto/mimo/glm/deepseek
    text_provider: str = "auto"

    # 第三方文生图 API
    image_api_key: str = ""
    image_api_base: str = "https://ai.gitee.com/v1"
    image_model: str = "z-image-turbo"

    # 腾讯机器人
    tencent_app_id: str
    tencent_app_secret: str
    tencent_token: str
    tencent_aes_key: str = ""

    # 服务
    host: str = "0.0.0.0"
    port: int = 8080

    class Config:
        env_file = ".env"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
