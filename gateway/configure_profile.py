"""Reconcile the PSY profile in place. Run as hermes with the Hermes Python environment."""
import fcntl
from io import StringIO
import os
from pathlib import Path
import pwd
import re
import sys
import tempfile

HOME = Path(os.environ.get("HERMES_HOME", str(Path.home()/".hermes")))
MODEL = "gpt-6.1-sol"


def instance_policy():
    from plugins.platforms.telegram.psy_instance import BOT, GROUP, OWNER
    return {
        "TELEGRAM_ALLOWED_USERS": str(OWNER), "TELEGRAM_HOME_CHANNEL": str(OWNER),
        "TELEGRAM_ALLOWED_CHATS": str(GROUP), "TELEGRAM_GROUP_ALLOWED_CHATS": str(GROUP),
        "TELEGRAM_REQUIRE_MENTION": "false", "TELEGRAM_GUEST_MODE": "false",
        "TELEGRAM_ALLOW_ALL_USERS": "false", "GATEWAY_ALLOW_ALL_USERS": "false",
        "TELEGRAM_REACTIONS": "true"
    }, str(BOT)


def stage_config(config, home, owner):
    model = config.get("model")
    if model is None or isinstance(model, str):
        model = {"default": model or MODEL}
        config["model"] = model
    if not isinstance(model, dict):
        raise ValueError("Invalid model configuration")
    if model.get("provider") != "openai-codex" or not model.get("default"):
        model["default"] = MODEL
    model["provider"] = "openai-codex"
    if model.get('base_url') not in (None,'','https://chatgpt.com/backend-api/codex'):
        raise ValueError('Review configured model and endpoint before recovery')
    auth=config.setdefault('auth',{})
    auth.update(adopt_external_logins=False,codex_login_flow='device_code')
    config['fallback_providers']=[]
    config.pop('fallback_model',None)
    config['allow_all_users']=False
    config['unauthorized_dm_behavior']='ignore'
    gateway=config.setdefault('gateway',{})
    gateway['allow_all_users']=False
    extra=gateway.setdefault('platforms',{}).setdefault('telegram',{}).setdefault('extra',{})
    extra['group_allow_admin_from']=[owner]
    extra.setdefault('group_user_allowed_commands',['start','status'])
    # Also set the top-level platform fields, which win over the nested gateway fields.
    platform = config.setdefault("platforms", {}).setdefault("telegram", {})
    platform.update(typing_indicator=True, gateway_restart_notification=False)
    plat_extra = platform.setdefault("extra", {})
    plat_extra.update(group_allow_admin_from=[owner], group_user_allowed_commands=["start", "status"],
                      local_mode=False, reactions=True)
    images = config.setdefault("image_gen", {})
    images.update(provider="openai-codex", model="gpt-image-2-medium", max_parallel_requests=1)
    images.setdefault("openai-codex", {})["model"] = images["model"]
    stt = config.setdefault("stt", {})
    stt.update(provider="chunked", enabled=True, language="ru", echo_transcripts=False)
    transcriber = home/"skills/media/audio-transcription/scripts/transcribe.py"
    stt.setdefault("providers", {}).setdefault("chunked", {}).update(
        type="command", local=True, timeout=21600, output_format="txt",
        command=f'/usr/bin/python3 {transcriber} {{input_path}} --output {{output_path}} --language {{language}}',
    )
    config.setdefault("privacy", {})["redact_pii"] = True
    config.setdefault("telemetry", {}).setdefault("shared_metrics", {}).update(enabled=False, send=False)
    config.setdefault("terminal", {})["cwd"] = str(home/"vault/projects")
    display = config.setdefault("display", {})
    display["background_process_notifications"] = "off"
    display.setdefault("platforms", {}).setdefault("telegram", {}).update(
        interim_assistant_messages=False, show_reasoning=False, long_running_notifications=False,
        cleanup_progress=True, streaming=False, suppress_warning_notifications=True,
        busy_ack_detail=False, busy_steer_ack_enabled=False, tool_progress="off",
    )
    return config


def atomic_save(path, content):
    with tempfile.NamedTemporaryFile('w',encoding='utf-8',dir=path.parent,delete=False) as target:
        temp=Path(target.name)
        try:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
            os.fchmod(target.fileno(),0o600)
            target.close()
            os.replace(temp,path)
            fd=os.open(path.parent,os.O_RDONLY)
            try:os.fsync(fd)
            finally:os.close(fd)
        finally:
            temp.unlink(missing_ok=True)


def reconcile(home):
    import copy
    import hermes_yaml as yaml
    from dotenv.parser import parse_stream
    from dotenv import dotenv_values
    home.mkdir(parents=True,mode=0o700,exist_ok=True)
    config_file,env_file=home/'config.yaml',home/'.env'
    with (home/'.psy-config.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        for path in (config_file,env_file,home/'telegram-token.pending'):
            if path.is_symlink() or path.exists() and (not path.is_file() or path.stat().st_uid!=os.geteuid()):
                raise ValueError('Unexpected configuration file ownership or type')
        raw_config=config_file.read_text() if config_file.exists() else ''
        before=yaml.safe_load(raw_config) if raw_config.strip() else {}
        if not isinstance(before,dict):
            raise ValueError('Invalid profile configuration')
        policy, bot_id=instance_policy()
        after=stage_config(copy.deepcopy(before), home, policy["TELEGRAM_ALLOWED_USERS"])
        env_text=env_file.read_text() if env_file.exists() else ''
        bindings=list(parse_stream(StringIO(env_text)))
        if any(b.error for b in bindings):
            raise ValueError('Unparseable environment configuration')
        values=dotenv_values(stream=StringIO(env_text),interpolate=False)
        token=values.get('TELEGRAM_BOT_TOKEN')
        pending=home/'telegram-token.pending'
        proposed=pending.read_text().strip() if pending.exists() else None
        if token and proposed and token!=proposed:
            raise ValueError('Existing bot credential and pending credential differ')
        active = token or proposed or ""
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{30,}",active) or active.partition(":")[0] != bot_id:
            raise ValueError("Telegram credential must match PSY_BOT_ID")
        if not token:
            policy['TELEGRAM_BOT_TOKEN']=proposed
        changed={k:v for k,v in policy.items() if values.get(k)!=v}
        if changed:
            # Preserve the verbatim source of every unrelated binding, including multiline secrets.
            retained=''.join(b.original.string for b in bindings if b.key not in changed)
            if retained and not retained.endswith('\n'):retained+='\n'
            env_text=retained+''.join(f'{key}={value}\n' for key,value in changed.items())
        # Compute and validate both prospective files before replacing either.
        rendered=yaml.safe_dump(after,allow_unicode=True,sort_keys=False) if after!=before else raw_config
        if changed:atomic_save(env_file,env_text)
        if rendered!=raw_config:atomic_save(config_file,rendered)
        for path in (env_file,config_file):path.chmod(0o600)
        if proposed and dotenv_values(env_file,interpolate=False).get('TELEGRAM_BOT_TOKEN')==proposed:
            pending.unlink()
        (home/'vault/projects').mkdir(mode=0o700,parents=True,exist_ok=True)
        return bool(changed or rendered!=raw_config)


if __name__=='__main__':
    os.umask(0o077)
    if os.geteuid()!=pwd.getpwnam('hermes').pw_uid:
        sys.exit('Run as hermes')
    sys.path.insert(0,str(HOME/'hermes-agent'))
    import hermes_bootstrap
    try:
        modified=reconcile(HOME)
    except Exception as exc:
        sys.exit('PSY configuration validation failed ('+type(exc).__name__+')')
    print('PSY configuration reconciled' if modified else 'PSY configuration already current')
