"""Format live status messages in Russian without queuing translations of counters."""
import re

from babel import Locale


_EN = Locale('en')
_RU = Locale('ru')
_NAMES = {label: _RU.languages.get(code, label) for code, label in _EN.languages.items()}
_NAMES.update({label.casefold(): _RU.territories.get(code, label) for code, label in _EN.territories.items()})
_NAMES.update({
    'English edition': 'английская редакция', 'country': 'страна', 'sector': 'отрасль',
    'related phrase': 'связанные фразы', 'risk-factor': 'факторы риска', 'keyword': 'ключевые слова',
    'Risk factors': 'Факторы риска', 'Keywords': 'Ключевые слова',
    'headline retries': 'повторный перевод заголовков', 'RSS recovery': 'восстановление RSS',
    'individual request': 'отдельный запрос', 'keyword translations': 'перевод ключевых слов',
    'headline backfill': 'перевод сохранённых заголовков', 'source translation': 'перевод источника',
    'run': 'Поиск', 'article': 'Статья', 'Finding candidates for': 'Ищем варианты перевода',
    'Generating': 'Готовим переводы', 'Checking': 'Проверяем переводы',
})

_FIXED = {
    'No translation pass requested.': 'Перевод ещё не запускался.',
    'Waiting for the current local-model task to finish.': 'Ожидание завершения текущей задачи локальной модели.',
    'Checking company news and official sources': 'Проверка новостей о компании и официальных источников',
    'Checking leadership and ownership records': 'Проверка сведений о руководстве и владельцах',
    'No independently approved candidate.': 'Ни один вариант не прошёл независимую проверку.',
}

# Values in named groups are localized only when they denote known UI labels.
# Company names, provider names, queries, model IDs and quoted descriptions stay verbatim.
_FORMATS = (
    (r'Legal Address differs from (.+?): (.+)', 'Юридический адрес отличается от адреса в {0}: {1}'),
    (r'Waiting behind run #(\d+) · (\d+) search(?:es)? ahead\.', 'В очереди за поиском №{0}. Поисков впереди: {1}.'),
    (r'Processing lead (\d+) of (\d+)', 'Обработка материала {0} из {1}'),
    (r'Searching company news · query (\d+) of (\d+)', 'Поиск новостей о компании · запрос {0} из {1}'),
    (r'Qwen · Checking headline translation (\d+) of (\d+)', 'Qwen · проверка перевода заголовка {0} из {1}'),
    (r'Checking missing month (\d{4}-\d{2}) · (.+)', 'Поиск за месяц без материалов: {0} · {1}'),
    (r'Searching (Perplexity|Google Gemini) · (.+)', 'Поиск в {0} · {1}'),
    (r'Searching (?P<branch>country|sector) news: (.+) \((?P<edition>[^()]+)\)(.*)',
     'Поиск новостей: {branch} · {1} ({edition}){3}'),
    (r'Unloading (.+) to free memory…', 'Выгружаем {0}, чтобы освободить память…'),
    (r'Loading (.+)…', 'Загружаем {0}…'),
    (r'(?P<scope>run|article) (\d+) · (.+) processing · reasoning off\.', '{scope} №{1} · работает {2} · режим рассуждений отключён.'),
    (r'(?P<task>.+) · (.+) processing · reasoning off\.', '{task} · работает {1} · режим рассуждений отключён.'),
    (r'Preparing risk-factor search phrases · (?P<language>.+)', 'Готовим поисковые фразы по факторам риска · {language}'),
    (r'(?P<verb>Finding candidates for|Generating|Checking) (\d+) (?P<kind>related phrase|risk-factor|keyword) translations · (?P<language>.+)',
     '{verb} · {kind} · {language}. Переводов: {1}.'),
    (r'Saved (\d+) of (\d+) (?P<kind>related phrase|risk-factor|keyword) translations · (?P<language>.+)',
     'Сохранено переводов: {0} из {1} · {kind} · {language}'),
    (r'(?P<kind>Risk factors|Keywords) · (?P<language>.+): retrying (\d+) unfinished items in smaller batches \((\w+)\)\.',
     '{kind} · {language}: повторная обработка небольшими группами. Осталось: {2}. Причина: {3}.'),
    (r'(?P<kind>Risk factors|Keywords) · (?P<language>.+): batch failed \((\w+)\); other batches were not cancelled\.',
     '{kind} · {language}: не удалось обработать группу ({2}). Остальные группы продолжают обрабатываться.'),
    (r'Search phrases · (?P<language>.+): interrupted \((\w+)\)\. Retry to resume\.',
     'Поисковые фразы · {language}: обработка прервана ({1}). Повторите попытку.'),
    (r'Translation worker stopped \((\w+)\)\. Retry missing translations to resume\.',
     'Служба перевода остановилась ({0}). Чтобы продолжить, запустите перевод пропущенных записей.'),
    (r'Translation interrupted \((\w+)\)\. Retry missing translations\.',
     'Перевод прерван ({0}). Запустите перевод пропущенных записей.'),
    (r'Queued (\d+) failed or inaccessible source checks\. Publisher recovery may use API credits\.',
     'Проверок источников в очереди на повторную попытку: {0}. Восстановление доступа может расходовать лимит API.'),
    (r"Search restarted as run #(\d+) using run #(\d+)'s company list\.",
     'Начат новый поиск №{0} по списку компаний из поиска №{1}.'),
    (r'Scoring refreshed: (\d+) articles and (\d+) weekly records updated\. No news requests made\.',
     'Оценки пересчитаны. Обновлено статей: {0}, недельных записей: {1}. Поиск новостей не запускался.'),
    (r'Queued (\d+) source checks \(up to eight new articles per company\)\.',
     'Проверок источников в очереди: {0} (до восьми новых статей по каждой компании).'),
    (r'Preliminary research started for (.+)\. Review the draft before saving\.',
     'Началась предварительная проверка компании {0}. Проверьте карточку перед сохранением.'),
    (r'Workbook intake: (\d+) drafts staged for research and review, (\d+) identical rows already staged\. No watchlist records were saved\.',
     'Импорт из Excel: новых карточек для проверки — {0}, ранее добавленных совпадающих строк — {1}. Список наблюдения пока не изменился.'),
    (r'Saved (\d+) leads\. Weekly scores now reflect the selected, eligible event evidence\.',
     'Сохранено материалов: {0}. Недельные оценки учитывают выбранные материалы, отвечающие критериям.'),
    (r'Updated (.+) after review\. It is now available for searches\.',
     'Карточка компании {0} обновлена после проверки. Теперь по ней можно выполнять поиск.'),
    (r'Added (.+) after review\. It is now available for searches\.',
     'Компания {0} добавлена после проверки. Теперь по ней можно выполнять поиск.'),
    (r'(.+) changed since you opened the list\. Compare the latest values before retrying\.',
     'Запись «{0}» изменилась с момента открытия списка. Перед повторным сохранением сверьте данные.'),
)
_FIELDS = {'Name': 'Название', 'Short Name': 'Краткое название', 'Country Registration': 'Страна регистрации',
           'Registration Number': 'Регистрационный номер', 'Tax Id': 'Налоговый номер', 'Legal Address': 'Юридический адрес',
           'Comment': 'Комментарий', 'Alias Notes': 'Примечания к названиям', 'Aliases': 'Варианты названия',
           'Top Management': 'Руководство', 'Primary Countries': 'Страны присутствия',
           'Affiliates': 'Аффилированные компании', 'Primary Industries': 'Отрасли', 'Key Owners': 'Владельцы'}


def status_ru(text, messages=None):
    """Return None for unknown prose. An optional interface catalog translates nested error details."""
    if not isinstance(text, str) or not text:
        return None
    catalog = messages or {}

    def known(message):
        if translated := _FIXED.get(message) or catalog.get(message):
            return translated
        for pattern, template in _FORMATS:
            if match := re.fullmatch(pattern, message, re.DOTALL):
                labels = {key: _NAMES.get(value, value) for key, value in match.groupdict().items()}
                return template.format(*match.groups(), **labels)
        return None

    if translated := known(text):
        return translated
    if match := re.fullmatch(r'(\d+) unfinished translations queued for retry when the search worker is idle\. (.+)', text, re.DOTALL):
        return (f'Незавершённых переводов в очереди: {match[1]}. Обработка продолжится, когда завершится поиск. '
                + (known(match[2]) or match[2]))
    if match := re.fullmatch(r'(.+): (\d+) (related phrase|risk-factor|keyword) translations still unapproved\. (.+)', text, re.DOTALL):
        return (f'Неподтверждённых переводов: {match[2]} · {_NAMES[match[3]]} · {_NAMES.get(match[1], match[1])}. '
                + (known(match[4]) or match[4]))
    if match := re.fullmatch(r'(.+) is too long', text):
        if field := _FIELDS.get(match[1]):
            return f'Слишком длинное значение в поле «{field}».'
    if text.startswith('Workbook import failed: '):
        detail = text.removeprefix('Workbook import failed: ')
        return 'Не удалось импортировать файл Excel: ' + (known(detail) or detail)
    # Worker prepends the company name; validation can prepend a description and a language.
    if found := re.search(r' · (?=Checking |Searching |Processing )', text):
        if translated := known(text[found.end():]):
            return text[:found.end()] + translated
    prefix, found, detail = text.rpartition(': ')
    if found and (translated := known(detail)):
        description, nested, label = prefix.rpartition(': ')
        return description + nested + _NAMES.get(label, label) + ': ' + translated
    return None
