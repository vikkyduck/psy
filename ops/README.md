# Подготовка узла PSY

Эти команды рассчитаны на отдельный узел Ubuntu 24.04 x86_64 с systemd, ядром 6.8+ и Landlock ABI 3+. Выполняйте их из корня проверенной копии репозитория. Поместите публичный исходный код в каталог, доступный на чтение пользователю hermes, например /opt/psy-source. Обычный старт бота выполняется только в последнем разделе. При восстановлении перенесите приватные данные и проверьте их владельцев до этого старта. Процедура с пустыми каталогами предназначена для нового экземпляра.

## Системные пользователи и файлы

```sh
sudo apt-get update
sudo apt-get install -y acl ca-certificates curl ffmpeg git openssl python3-venv util-linux
sudo bash ops/prepare-host
```

Пользователь hermes владеет профилем /home/hermes/.hermes и запускает шлюз. Отдельные UID psy-upload, psy-access, psy-deliver и psy-asr работают с загрузкой файла, проверками Telegram, доставкой и офлайн-декодером соответственно. prepare-host создает файл пустой очереди и настраивает POSIX ACL для будущих файлов. В пространстве монтирования службы каталог медиа виден как /srv/psy-media, очередь как /srv/psy-queue, сводка прогресса как /srv/psy-progress. Родительские каталоги профиля закрыты. Домашний каталог сервисов, кроме hermes, недоступен. Права уже существующих записей при переносе восстанавливают вместе с их ACL.

На чистой тестовой VM без Telegram можно сразу выполнить:

```sh
sudo bash ops/install-runtime
sudo bash ops/test-host
PYTHONPATH=ops/libexec python3 -m unittest discover -s ops/tests -v
```

test-host запускает только краткоживущие тестовые процессы. Они создают искусственную запись, читают ее с правами декодера и проверяют совместный доступ отправителя и загрузчика к результату. При существующих профиле и запущенных рабочих службах подготовка прерывается. Без бинарных зависимостей базовый тест проверяет загрузку описаний unit и доступ к данным. Для проверки путей исполняемых файлов используется test-host --units.

## Адрес и разрешенные участники

На целевом узле заполните /etc/psy/instance.env, созданный из instance.env.example. В этом файле хранятся только параметры экземпляра. Он принадлежит root, доступ на чтение есть у группы hermes.

| Переменная | Что указать |
|---|---|
| PSY_OWNER_ID | Положительный числовой Telegram ID владельца |
| PSY_GROUP_ID | Отрицательный числовой ID рабочего группового чата |
| PSY_BOT_ID | Числовой ID бота из BotFather |
| PSY_BOT_USERNAME | Имя бота без символа @ |
| PSY_PUBLIC_URL | Публичный HTTPS origin Mini App без пути, с портом при необходимости |
| PSY_LISTEN_HOST | Адрес привязки веб-сервера, по умолчанию 0.0.0.0 |
| PSY_LISTEN_PORT | Порт сервера загрузок, по умолчанию 9443 |

Шлюз, сервер, worker и сокетные брокеры получают значения через systemd EnvironmentFile. Доступ в группу проверяется перед запуском Mini App и повторно перед отправкой результата.

## Hermes и распознавание

Установите закрепленный в [инструкции восстановления](../docs/RESTORE.md) коммит Hermes и примените gateway/apply.py. Launcher должен появиться в /home/hermes/.hermes/hermes-agent/.hermes/bin/hermes. Команду установки Hermes запускают в неинтерактивном режиме от hermes.

```sh
repo=$(pwd -P)
checkout=/home/hermes/.hermes/hermes-agent
sudo -Hu hermes python3 "$repo/gateway/apply.py" apply "$checkout"
sudo -Hu hermes python3 "$repo/gateway/apply.py" verify "$checkout"
sudo -Hu hermes bash -c 'cd "$1" && python3 -c "import hermes_bootstrap, pm; pm.sync_venv([\"telegram\", \"stt-whisper\"], explicit=True)"' bash "$checkout"
```

Для отдельного офлайн-декодера и Mini App разверните 2 окружения Python 3.12. Файлы фиксируют версии, установленные в рабочих средах. Для автономной установки сохраните совместимые wheel-файлы и используйте pip install с --no-index --find-links.

```sh
sudo python3 -m venv /opt/psy-upload-venv
sudo /opt/psy-upload-venv/bin/pip install -r media/requirements.lock.txt
sudo /opt/psy-upload-venv/bin/pip check
sudo python3 -m venv /opt/psy-asr-venv
sudo /opt/psy-asr-venv/bin/pip install -r media/asr-requirements.lock.txt
sudo /opt/psy-asr-venv/bin/pip check
```

Загрузите 4 файла модели в /opt/psy-asr-model по командам и контрольным суммам из [инструкции по медиа](../media/README.md). Каталог и файлы должны быть доступны на чтение hermes и psy-asr. Независимая проверка SHA-256 входит в verify-ready. Разверните сервисные файлы и профиль после установки Hermes.

```sh
sudo bash ops/install-runtime --new-profile
sudo bash ops/test-host --units
```

Ключ --new-profile ставит публичный SOUL.md в новый профиль и при необходимости сохраняет первоначальный SOUL.md от Hermes рядом как SOUL.md.before-psy. При восстановлении личного профиля используйте install-runtime без ключа. Установщик располагает копию навыка и скрипта по пути skills/media/audio-transcription, исходники Mini App в /opt/psy-upload и независимый манифест границы группы в /usr/local/lib/psy-group. При каждом старте шлюза preflight сверяет файлы адаптера, политики OAuth и проверки экземпляра с манифестом релиза.

## Локальные учетные данные

Для нового бота введите полученный в BotFather токен через скрытый ввод. Запускайте команду в личном интерактивном терминале. Файл telegram-token.pending принадлежит hermes, режим 0600. Конфигуратор переносит значение в закрытый .env и проверяет совпадение с PSY_BOT_ID. Восстановленный профиль с действующим .env пропускает шаг скрытого ввода.

```sh
sudo -Hu hermes python3 -c 'from getpass import getpass; from pathlib import Path; import os; os.umask(0o077); p=Path.home()/".hermes/telegram-token.pending"; t=getpass("Telegram token: "); f=p.open("x"); f.write(t.strip()); f.close()'
repo=$(pwd -P)
sudo -Hu hermes bash -ec 'set -a; . /etc/psy/instance.env; set +a; exec python3 "$1/gateway/configure_profile.py"' bash "$repo"
sudo bash ops/stage-credential
sudo -Hu hermes /home/hermes/.hermes/hermes-agent/.hermes/bin/hermes auth add openai-codex
```

При первичном входе завершите device-code в своем браузере. Для запуска мониторинга нужен один предпочтительный вход manual:device_code с priority 0 в credential_pool провайдера openai-codex. Дополнительные входы получают следующий приоритет. При точном восстановлении профиль может уже содержать актуальный auth.json. Сначала проверьте его на основном узле. Одновременное обновление одного и того же refresh token двумя машинами может аннулировать текущий вход.

## Сертификат и проверка перед стартом

Для адреса с DNS именем создайте A/AAAA записи, настройте входящие TCP 80 для ACME HTTP-01 и порт из PSY_PUBLIC_URL для загрузки. Для встроенного HTTPS-сервера по умолчанию используется 9443. Для этой команды 80 должен быть свободен при выдаче и продлении. Certbot запрашивает контакт и согласие с условиями у администратора. Вместо UPLOAD_DOMAIN укажите свое DNS имя без схемы или порта в переменной окружения терминала.

```sh
sudo /opt/psy-upload-venv/bin/certbot certonly --standalone --cert-name psy-upload-ip -d "$UPLOAD_DOMAIN"
```

Для Mini App с HTTPS по публичному IP укажите свой IP в UPLOAD_IP и используйте этот запрос сертификата. Профиль shortlived выпускает сертификат на 6 дней, поэтому таймер продления важен и при работе без DNS. [Инструкция центра сертификации](https://letsencrypt.org/2026/03/11/shorter-certs-certbot)

```sh
sudo /opt/psy-upload-venv/bin/certbot certonly --standalone --cert-name psy-upload-ip --preferred-profile shortlived --ip-address "$UPLOAD_IP"
```

После выдачи сертификата выполните общие проверки.

```sh
sudo /usr/local/libexec/psy-upload-deploy-cert --stage
sudo bash ops/verify-ready
```

При переносе существующей установки подготовьте действующую линию сертификата Certbot с именем psy-upload-ip для выбранного адреса. Ключ остается в /etc/psy-upload/tls и доступен группе psy-upload. deploy-cert проверяет соответствие ключа цепочке и атомарно переключает current. При продлении Certbot вызывает тот же скрипт и загружает обновление сигналом HUP. Ключ --stage выполняет подготовку до старта. Проверку доступности публичного HTTPS-адреса проводите с внешней машины после включения.

## Включение и эксплуатация

Если восстанавливаете прежнего бота, отключите его опрос Telegram и обновление OAuth на старом узле перед переключением. Контрольная проверка verify-ready ожидает остановленные и выключенные службы. Включите сокеты и обработчики, затем шлюз. После успешной проверки соединения включите таймеры.

```sh
sudo systemctl enable --now psy-member.socket psy-group-upload.socket psy-asr.service psy-upload.service psy-upload-worker.service
sudo systemctl enable --now hermes-gateway.service
systemctl is-active hermes-gateway psy-upload psy-upload-worker psy-asr
sudo systemctl start psy-codex-auth.service
sudo systemctl enable --now psy-codex-auth.timer psy-healthcheck.timer psy-upload-health.timer psy-vault-archive.timer psy-upload-cert-renew.timer psy-alerts.timer
systemctl list-timers 'psy-*'
```

- psy-codex-auth раз в час проверяет подписку. Группа, монитор и Hermes передают продление выбранного входа модулю `hermes_cli/psy_codex_policy.py`. Он обновляет совместимые записи одного входа за одну операцию и сохраняет паузу после сбоя соединения для всех трех путей. Если сервер мог уже принять одноразовый ключ, повторный обмен этим ключом блокируется. После получения новых учетных данных ПСИ сохраняет их в закрытом локальном файле до завершения записи. При перезапуске он завершает запись и удаляет временную копию. Если владелец должен подтвердить новый вход, ПСИ присылает кнопку в личный чат. В этом чате также доступна команда `/codex_login`. После входа ПСИ проверяет модель коротким запросом и сразу возобновляет задания, ожидавшие авторизации.
- psy-healthcheck опрашивает управляющий Unix-сокет шлюза и Telegram getWebhookInfo. По серии сбоев восстанавливает прием с ограничением частоты перезапусков.
- psy-upload-health раз в минуту проверяет HTTPS /healthz, worker, ASR и состояние очереди. После 3 последовательных сбоев запускает восстановление нужной службы. Метрики ожидания доставки, прогресса и свободного места отдельно сохраняются в локальном состоянии.
- psy-vault-archive сохраняет сообщения и сгенерированные файлы в локальном vault. Для переживания потери сервера нужна отдельная приватная копия данных.
- psy-upload-cert-renew раз в 12 часов проверяет необходимость продления. На узле с общей службой ACME согласуйте выбранный способ продления для этого адреса.
- Исходники psy-audio-priority сохранены для воспроизводимости истории конфигурации. Таймер контролировал факультативную сборку Telegram Bot API. На исходном узле он выключен, сборка отсутствует. install-runtime пропускает эти unit. Штатная очередь распознавания работает через psy-asr и psy-upload-worker.

## Обновление работающего узла

install-runtime готовит только остановленный узел. На узле, который уже обслуживает группу, используйте ops/update_live.py из проверенной копии репозитория, например /opt/psy-source после git pull. Сначала посмотрите план, затем примените.

```sh
sudo python3 ops/update_live.py plan
sudo python3 ops/update_live.py apply
```

Сценарий раскладывает те же файлы, что install-runtime. Изменения Hermes он получает из gateway/hermes.patch на закрепленном коммите. Если Hermes на узле стоит на другом коммите или содержит неизвестные релизу правки, сценарий отказывается работать. До изменений он проверяет:

- совпадение group-manifest.json с manifest.json;
- что значения /etc/psy/instance.env принимают модули проверки параметров.

Профиль владельца, навыки, instance.env и закрытые файлы не меняются. Drop-in, которые только повторяют настройки поставляемого unit, сценарий снимает. Остальные он оставляет как локальные и показывает в плане.

Прежние версии файлов сохраняются в /var/lib/psy-deploy. Затем сценарий:

1. Запускает preflight.
2. Перезапускает только затронутые службы: шлюз последним.
3. Ждет подключения шлюза к Telegram.

При любом сбое он возвращает прежние файлы. Пока идет групповое задание или распознавание записи, сценарий отказывается без ключа --force. Примененный коммит записывается в /etc/psy/deployed.

Проверки, которые требуют Telegram, перечислены в [инструкции восстановления](../docs/RESTORE.md). Они включают настройку кнопки, пробную загрузку и подтверждение результата в целевом чате.


## Закрытые копии и внешний наблюдатель

[RECOVERY.md](../docs/RECOVERY.md) описывает ночную зашифрованную копию, проверку архива и возврат SQLite, ключа подписания, OAuth, глав и медиа. `ops/install-backup` устанавливает код и unit-файлы, не начинает передачу и не создает ключи. После настройки внешнего backup-only доступа проверяется одна настоящая копия, затем включается таймер.

[OBSERVABILITY.md](../docs/OBSERVABILITY.md) описывает отдельный узел для личных уведомлений владельцу при недоступности основной машины. `ops/install-observer` и `ops/stage-observer.py` ставят эту часть без автоматической отправки. При смене адреса обновляют как основную, так и внешнюю конфигурацию.

На восстановленном из обычных файлов свободном узле `ops/restore-permissions` возвращает владельцев и ACL по именам учетных записей. Он не запускает службы. Эти команды не выполняют на работающем ПСИ.
