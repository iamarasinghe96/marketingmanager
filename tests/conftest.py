"""The tests exercise both campaigns, so they run with every campaign switched on,
whatever campaign.yaml says today (e.g. LushNote is off until the owner starts it)."""
import bot.config as config

real_load_config = config.load_config


def load_config_all_enabled():
    settings, campaigns, secrets = real_load_config()
    campaigns = {slug: c.model_copy(update={"enabled": True}) for slug, c in campaigns.items()}
    return settings, campaigns, secrets


config.load_config = load_config_all_enabled
