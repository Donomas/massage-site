# Сервер редактора сайта

Сервер на Python 3.11+ хранит черновики новостей, акций, услуг и программ в SQLite, а медиафайлы в закрытом каталоге. На GitHub Pages публикуется подтверждённый снимок выбранного раздела. Публикация отправляет один Git-коммит с HTML, JSON, картой сайта и вложениями выбранных карточек. GitHub Pages затем обрабатывает коммит своим обычным способом.

## Настройка

Для генерации хеша пароля запустите интерактивно на сервере

```sh
python3 server/app.py --set-password
```

Пароль вводится скрыто, команда выводит scrypt-хеш. Сохраните хеш в закрытом файле окружения сервиса. Переменная `NEWS_DATA_DIR` должна указывать за пределы checkout. Доступ к данным нужен только пользователю сервиса.

| Переменная | Назначение |
| --- | --- |
| `NEWS_PASSWORD_HASH` | Сгенерированный scrypt-хеш, обязательный |
| `NEWS_USERNAME` | Логин редактора, по умолчанию `admin` |
| `NEWS_DATA_DIR` | Закрытые SQLite и файлы, по умолчанию `~/.local/share/massage-news` |
| `NEWS_MEDIA_QUOTA_MB` | Общий лимит приватных загрузок в МиБ, по умолчанию `512`. При заполнении API возвращает 507 `storage_full` |
| `ALLOWED_ORIGIN` | Точный origin редактора, по умолчанию `https://donomas.github.io`, без пути и завершающего `/` |
| `GITHUB_BRANCH` | Ветка публикации, по умолчанию `main` |
| `NEWS_REPO_DIR` | Локальный clone с настроенным SSH deploy key с правом записи. При наличии выбирает публикацию Git SSH |
| `GITHUB_TOKEN` | Альтернатива SSH, fine-grained token с Contents write для одного репозитория |
| `GITHUB_REPO` | Репозиторий REST-публикации, по умолчанию `Donomas/massage-site` |

Файл окружения `/etc/massage-news.env` для SSH-варианта

```ini
NEWS_USERNAME=admin
NEWS_PASSWORD_HASH=scrypt$16384$8$1$REPLACE_WITH_GENERATED_HASH
NEWS_DATA_DIR=/var/lib/massage-news
NEWS_MEDIA_QUOTA_MB=512
NEWS_REPO_DIR=/opt/massage-site/repository
GITHUB_BRANCH=main
ALLOWED_ORIGIN=https://donomas.github.io
```

Замените пример хеша полным результатом команды. Ограничьте чтение файла окружения пользователем сервиса и администратором. В clone настройте origin на нужный репозиторий и SSH-ключ. Доверенный ключ хоста GitHub следует сверить с официальными опубликованными fingerprint перед добавлением в known_hosts. Команда `git ls-remote origin` от пользователя сервиса должна успешно прочитать ветку без интерактивного ввода. Сервер создаёт Git-объекты через временный index в приватном каталоге и сохраняет рабочие файлы clone. Обычный push отклоняет конфликтующие изменения ветки.

## Запуск systemd

Пример `/etc/systemd/system/massage-news.service` при установке backend в `/opt/massage-news/server`. Пользователь `massage-news` должен иметь доступ к SSH-ключу, clone и каталогу данных. Пути можно адаптировать к фактической установке.

```ini
[Unit]
Description=Massage news editor API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=massage-news
Group=massage-news
WorkingDirectory=/opt/massage-news
EnvironmentFile=/etc/massage-news.env
ExecStart=/usr/bin/python3 /opt/massage-news/server/app.py
Restart=on-failure
RestartSec=3
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/massage-news /opt/massage-site/repository

[Install]
WantedBy=multi-user.target
```

Сервис слушает `127.0.0.1:8787`. Открывайте API через reverse proxy после настройки HTTPS. На публичном HTTP до выпуска сертификата достаточно отдельного webroot для ACME. После подготовки пользователя, каталогов и файла окружения активируйте сервис

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now massage-news
sudo systemctl status massage-news
```

Для SSH-ключа подходит отдельный каталог `/var/lib/massage-news/.ssh`. Если требуется конкретный ключ, настройте git `core.sshCommand` в clone, используя абсолютный путь к ключу. В REST-варианте `NEWS_REPO_DIR` удаляется из окружения, а `GITHUB_TOKEN` добавляется туда же как секрет.

## HTTPS reverse proxy

Настройте TLS-сертификат для адреса API. Пример блока nginx после установки сертификата

```nginx
server {
    listen 443 ssl;
    server_name news-api.example.org;
    ssl_certificate /etc/letsencrypt/live/news-api.example.org/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/news-api.example.org/privkey.pem;
    client_max_body_size 30m;

    location / {
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 900s;
        proxy_send_timeout 120s;
        proxy_request_buffering on;
    }
}
```

API предполагает этот reverse proxy для TLS и определения IP входа. Поле `X-Real-IP` учитывается только от loopback. Клиент редактора должен обращаться к HTTPS-адресу API. В `ALLOWED_ORIGIN` укажите origin страницы редактора, включая порт при локальной проверке. CORS разрешает только этот точный origin, заголовок Authorization и методы API. Cookies не используются.

## API

Ответы JSON. Ошибки имеют поля `{error, code}`. Кроме `/health` и `/login`, запросы требуют `Authorization: Bearer TOKEN`. Токен живёт восемь часов, хранится в памяти сервера и становится недействительным после выхода или перезапуска. Фронтенд хранит его в sessionStorage. `GET /media/…` также требует токен, для предпросмотра фронтенд получает файл через fetch и создаёт Blob URL.

| Запрос | Тело и результат |
| --- | --- |
| `GET /health` | `{ok:true}` |
| `POST /login` | `{username,password}` → `{token}` |
| `POST /logout` | `{}` → `{ok:true}` |
| `GET /draft?section=services` | `{cards,revision}`, начальная revision раздела равна 0 |
| `PUT /draft` | `{cards,revision,section}` → сохранённые `{cards,revision+1}`. Устаревшая revision выбранного раздела даёт 409 |
| `POST /upload` | `{name,data}` с raw base64 → HTTP 201 и `{id,url,kind,name}` |
| `GET /media/{id.ext}` | Байты авторизованного вложения |
| `POST /publish` | `{revision,section}` → `{commit,url}`. Нужна текущая сохранённая revision выбранного раздела |
| `GET /publish-status?section=services` | `{commit,url,revision,status}`. status равен `submitted` либо `never` |

Разрешены только `news`, `offers`, `services`, `programs`. Пропущенный section выбирает news и сохраняет совместимость с исходным API. У GET-запросов раздел находится в query, у PUT и POST — в JSON-теле. Неизвестный раздел, пустая строка и повторённый query section дают HTTP 400. Каждому разделу принадлежат отдельные черновик, revision и статус публикации. Все публикации сериализуются одним lock, чтобы коммиты разных разделов не потеряли изменения друг друга.

Карточка содержит `{id,title,text,date,media}`. Только услуги дополнительно допускают необязательное поле `duration`, строку до 80 символов. Поля цен не принимаются. ID допускает буквы, цифры, дефисы и подчёркивания. Дата имеет вид `YYYY-MM-DD`, показывается на публичных страницах новостей и акций, а в услугах и программах хранится внутри карточки. До 50 карточек, заголовок до 160 символов, текст до 10000, до пяти файлов на карточку. Порядок массива задаёт порядок публикаций. Удаление карточки и перестановка сохраняются через PUT всего массива с текущей revision. Вложение возвращается загрузчиком и передаётся в карточку без изменений. Имя файла служит подписью изображения. ID вложения — UUID hex, URL вида `/media/{id}.{ext}`, kind — `image` либо `video`. Поддерживаются JPEG, PNG, WebP, GIF, MP4 и WebM с проверкой сигнатур. Предел одного файла — 20 МиБ, JSON-запроса — 30 МиБ.

Ограничение входа составляет пять попыток за пять минут с одного IP и 30 за минуту суммарно. Счётчики ограничены 256 IP, число активных сессий ограничено 128. Общая квота файлов по умолчанию составляет 512 МиБ. Проверка объёма и запись сериализованы, поэтому параллельные загрузки соблюдают общий лимит. Загруженные файлы сохраняются, включая вложения удалённых из черновика карточек. При заполнении администратор может проверить и удалить ненужные приватные файлы при остановленном сервисе или увеличить квоту. Запросы, заголовки и секреты не журналируются обработчиком. Данные сохраняются локально, резервную копию SQLite и media следует делать совместно при остановленном сервисе.

В GitHub отправляются `content/{section}.json`, `{section}.html`, `sitemap.xml` и новые `assets/{section}/{id.ext}` только из публикуемых карточек. В шаблоне news нужны единственные `<!-- NEWS:START -->` и `<!-- NEWS:END -->`, а в остальных разделах — `<!-- CONTENT:START -->` и `<!-- CONTENT:END -->`. Каждой странице нужны один meta robots и HTTPS canonical. Контент между маркерами заменяется экранированным HTML. Непустой раздел получает index и запись в sitemap, пустой — noindex и удаление своей записи. Остальные страницы и URL карты сайта сохраняются через базовое дерево Git. Ранее опубликованные вложения остаются в репозитории и истории.

При публикации услуг JSON-LD обновляется из тех же карточек. Блок с id `services-schema` сохраняет Person, пересоздаёт ItemList и Service, включая порядок, названия, описания, длительность и ссылки на текущие ID карточек. Удалённые услуги исчезают и из HTML, и из JSON-LD. В JSON-LD символ `<` экранируется, чтобы текст карточки не мог закрыть script.

Публичный JSON содержит `{cards,revision,digest,section}`. URL медиа заменяются на относительные `./assets/{section}/…`. SHA256 digest описывает раздел и канонический снимок карточек. При повторе после потери ответа сервис сверяет выбранные опубликованные JSON-карточки, раздел и digest, возвращает текущий коммит ветки и восстанавливает локальный статус. В случае конфликта или сбоя черновик и последний подтверждённый статус сохраняются. Оставшиеся недостижимые Git-объекты не публикуются в ветку.

При первом запуске новой версии миграция SQLite одной транзакцией переносит прежние новости, revision и статус публикации в таблицу section_state. Прежняя таблица state и таблица медиа сохраняются. Повторный запуск использует section_state и не перезаписывает отредактированные новости. Для новых разделов сервер при запуске читает `content/services.json`, `content/offers.json` и `content/programs.json` рядом с папкой server. `Store.seed_section(section,cards)` добавляет исходный массив с revision 0 только при отсутствии раздела. После сохранения, удаления или перестановки карточек seed повторно не применяется. На рабочем сервере с `/root/site/server/app.py` файлы seed размещаются в `/root/site/content/`.

Статус `submitted` подтверждает отправку коммита в GitHub. Готовность сборки GitHub Pages проверяется отдельно в репозитории. Этот документ описывает установку и сам по себе не подтверждает развёртывание сервиса.

## Проверка

```sh
python3 -m unittest discover -s server -v
```

Тесты проверяют HTTP-аутентификацию, CORS, ограничение входа, закрытые загрузки и квоту, конкуренцию сохранения, миграцию заполненной прежней базы, разделение ревизий и статусов, seed, экранирование HTML и JSON-LD услуг, переключение sitemap, атомарность GitHub REST с подменённым транспортом, а также реальную Git-публикацию в локальный bare-репозиторий и восстановление после потерянного ответа push. Для тестов нужен установленный git и доступ к loopback. Настоящий GitHub не вызывается.
