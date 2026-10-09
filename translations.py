"""Central, keyed interface catalogs. Canonical game data is never translated."""
import json
import logging
import re
from contextvars import ContextVar
from pathlib import Path

logger = logging.getLogger(__name__)
DEFAULT_LANGUAGE = 'en'
CATALOG_DIR = Path(__file__).resolve().parent / 'locales'
TRANSLATIONS = {path.stem: json.loads(path.read_text(encoding='utf-8'))
                for path in sorted(CATALOG_DIR.glob('*.json'))}
SUPPORTED_LANGUAGES = frozenset(TRANSLATIONS)
CURRENT_LANGUAGE = ContextVar('duellium_interface_language', default=DEFAULT_LANGUAGE)
LANGUAGE_NAMES = {'en': 'English', 'fr': 'Français'}
LANGUAGE_INPUTS = {'en': 'en', 'english': 'en', 'anglais': 'en',
                   'fr': 'fr', 'french': 'fr', 'français': 'fr', 'francais': 'fr'}
COMMAND_ALIASES = {'fr': {
    'profile': 'profil', 'house': 'maison', 'train': 'entrainer',
    'spellbook': 'sorts', 'spell': 'sort', 'learn': 'apprendre',
    'answer': 'reponse', 'cancellearn': 'annulerapprentissage',
    'teach': 'enseigner', 'askhelp': 'demanderaide', 'training': 'entrainement',
    'canceltraining': 'annulerentrainement', 'accept': 'accepter', 'decline': 'refuser',
    'dodge': 'esquive', 'leaderboard': 'classement', 'globalleaderboard': 'classementglobal',
    'houseleaderboard': 'classementmaisons', 'language': 'langue',
    'setlanguage': 'definirlangue', 'help': 'aide',
}}
INPUT_ALIASES = {'fr': {
    'house': {'gryffondor': 'gryffindor', 'serpentard': 'slytherin',
              'serdaigle': 'ravenclaw', 'poufsouffle': 'hufflepuff'},
    'stat': {'puissancemagique': 'magic_power', 'vitesse': 'speed',
             'agilite': 'agility', 'agilité': 'agility'},
}}


def translate(language, key, **kwargs):
    english = TRANSLATIONS[DEFAULT_LANGUAGE].get(key, key)
    template = TRANSLATIONS.get(language, {}).get(key, english)
    try:
        return template.format(**kwargs)
    except (KeyError, ValueError, IndexError, AttributeError):
        logger.warning('Invalid translation placeholders for key %s; using English', key)
        try:
            return english.format(**kwargs)
        except (KeyError, ValueError, IndexError, AttributeError):
            return english


def tr(key, **kwargs):
    return translate(CURRENT_LANGUAGE.get(), key, **kwargs)


def house_display(key):
    return tr('house.' + key)


def stat_display(key):
    return tr('stat.' + key)


def combat_stat_display(key):
    return tr('combat_stat.' + key)


def normalize_input(category, value):
    aliases = INPUT_ALIASES.get(CURRENT_LANGUAGE.get(), {}).get(category, {})
    return aliases.get(value, value)


# Display lookups keep canonical statuses, objective labels, and shuffled quiz
# content intact. They never alter saved player fields or answer identities.
DISPLAY_KEYS = {value: key for key, value in TRANSLATIONS['en'].items()
                if key.startswith(('status.', 'combat_stat.', 'quiz.'))}


def localized_label(value):
    if value in DISPLAY_KEYS:
        return tr(DISPLAY_KEYS[value])
    if value.endswith(' Hits'):
        return tr('training.hits', spell=value[:-5])
    return value


def quiz_display(value):
    return tr(DISPLAY_KEYS[value]) if value in DISPLAY_KEYS else value


def parameter_display(name):
    key = 'help.parameter.' + name
    return tr(key) if key in TRANSLATIONS[DEFAULT_LANGUAGE] else name


def command_signature(signature):
    return re.sub(r'\b[a-z_]+\b', lambda match: parameter_display(match.group()), signature)
