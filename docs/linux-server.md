# Постоянный запуск на Linux-сервере

Установка собственного экземпляра бота как пользовательского сервиса systemd.
Нужны доступ к интернету, Python 3.12+, Git и учётная запись на Linux-сервере.
Предварительно создайте приложение и настройте права по [инструкции](setup.md#приложение-discord).

## Установка

Команды выполняются от обычного пользователя, не от root:

```sh
mkdir -p ~/apps
git clone https://github.com/leonovenotik-spec/discord-speaker.git ~/apps/discord-speaker
cd ~/apps/discord-speaker
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

Если в системе нет `ensurepip`, можно установить пакет `python3-venv` средствами
своего дистрибутива. Вариант без прав администратора — официальный zipapp virtualenv:

```sh
mkdir -p ~/.cache/discord-speaker
curl -fsSL https://bootstrap.pypa.io/virtualenv.pyz -o ~/.cache/discord-speaker/virtualenv.pyz
python3 ~/.cache/discord-speaker/virtualenv.pyz --no-periodic-update ~/apps/discord-speaker/.venv
~/apps/discord-speaker/.venv/bin/python -m pip install -r ~/apps/discord-speaker/requirements.txt
```

## Настройка токена

В консоли сервера выполните:

```sh
python3 ~/apps/discord-speaker/scripts/configure.py
```

Вставь токен бота. Ввод токена не отображается. ID сервера не требуется:
бот работает на всех серверах, куда его добавили.
Скрипт сохраняет настройки в `~/.config/discord-speaker/bot.env` с правами `600`
и не перезаписывает уже существующий файл. Токен не попадает в историю команд.
Чтобы изменить настройки позже, открой файл в редакторе на сервере.

## Сервис и автозапуск

```sh
mkdir -p ~/.config/systemd/user
install -m 644 ~/apps/discord-speaker/deploy/discord-speaker.service ~/.config/systemd/user/discord-speaker.service
systemctl --user daemon-reload
systemctl --user enable --now discord-speaker.service
```

Чтобы сервис запускался при загрузке компьютера и продолжал работать после выхода
из SSH, включи linger для своего пользователя:

```sh
loginctl enable-linger "$USER"
```

Если система требует права администратора, эту команду должен выполнить владелец
сервера с `sudo`. Настройки питания ноутбука должны позволять ему работать постоянно.
Сервис работает, пока сервер включён и подключён к интернету.

## Проверка и управление

```sh
systemctl --user status discord-speaker.service
journalctl --user -u discord-speaker.service -n 50 --no-pager
loginctl show-user "$USER" -p Linger
```

После записи `READY` проверь `/panel` в Discord: речь должна быть слышна в твоём
голосовом канале, микрофон должен отключиться на время речи и восстановиться после неё.
Бот останется в канале до выхода вызвавшего участника или команды `/leave`.
Для обычных сообщений включи Message Content Intent в Developer Portal,
перезапусти сервис и выбери канал командой `/textchannel channel:…`.
Разрешённый intent определяется при запуске; отдельная переменная среды не нужна.
Перезапуск сервиса: `systemctl --user restart discord-speaker.service`.
Остановка и отключение автозапуска: `systemctl --user disable --now discord-speaker.service`.
При аварийном завершении и отключённом хосте серверный мут можно снять вручную в Discord.

Для обновления сначала убедись, что бот ничего не озвучивает:

```sh
cd ~/apps/discord-speaker
git pull --ff-only
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
systemctl --user restart discord-speaker.service
```

Состояние голоса, скорости и восстановления хранится в `~/.local/share/discord-speaker`.
Внутри него данные разделены по серверам и участникам. Доступ открыт всем по умолчанию;
настройка роли выполняется управляющим сервером через `/access`.
Токен и состояние находятся вне репозитория. Не прикладывай файл `bot.env` к отчётам об ошибках.
