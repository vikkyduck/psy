"""Real native Hermes acceptance. No Telegram adapter or outbound sender is started."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import uuid


def main():
    checkout, workspace = map(Path, sys.argv[1:3])
    sys.path.insert(0, str(checkout))
    # The official launcher activates the managed dependencies before app imports.
    import hermes_bootstrap
    workspace.mkdir(mode=0o700, exist_ok=True)
    os.chdir(workspace)
    from hermes_cli.config import load_config
    from hermes_state import SessionDB
    from run_agent import AIAgent
    config = load_config()
    assert config['model']['provider'] == 'openai-codex'
    assert config['compression']['enabled'] and config['compression']['threshold'] == 0.5
    marker = 'КВАРЦ-' + uuid.uuid4().hex[:10]
    source = workspace / 'full-source.txt'
    raw = ('\n'.join(f'Строка {n}: исходный материал без сокращения.' for n in range(1, 18002)) + '\n' + marker + '\n').encode()
    source.write_bytes(raw)
    expected_hash = hashlib.sha256(raw).hexdigest()
    sid = 'psy-standard-acceptance-' + uuid.uuid4().hex[:10]
    db_path = workspace / 'acceptance-state.db'
    db = SessionDB(db_path)
    db.create_session(session_id=sid, source='telegram', model=config['model']['default'])
    question = (f'Прочитай весь файл {source} инструментами. Вычисли SHA-256 всех байтов, '
                f'точное число строк и код в последней строке. Запиши эти 3 значения в {workspace / "result.txt"} '
                'и повтори их в ответе. Ничего не отправляй в Telegram.')
    def agent():
        return AIAgent(model=config['model']['default'], provider='openai-codex',
                       enabled_toolsets=['hermes-telegram'], quiet_mode=True,
                       reasoning_config={'effort': 'max'}, session_id=sid, session_db=db,
                       platform='telegram', skip_memory=True, skip_background_review=True,
                       skip_context_files=True, fallback_model=None, cwd=str(workspace))
    first = agent()
    try:
        answer = first.run_conversation(user_message=question)
    finally:
        first.close()
    content = (workspace / 'result.txt').read_text()
    assert expected_hash in content and marker in content and '18002' in content, 'full file result mismatch'
    with sqlite3.connect(db_path) as connection:
        assert connection.execute('SELECT count(*) FROM messages WHERE role=? AND content=?', ('user', question)).fetchone()[0] == 1
        tool_count = connection.execute("SELECT count(*) FROM messages WHERE role='tool'").fetchone()[0]
        assert tool_count > 0, 'native tools did not execute'
    history = db.get_messages_as_conversation(sid)
    assert any(m.get('role') == 'user' and m.get('content') == question for m in history)
    second = agent()
    try:
        reply = second.run_conversation(user_message='Какой код был в последней строке? Ответь по сохраненной истории, не открывай файл снова.', conversation_history=history)
    finally:
        second.close()
    response = str(reply.get('final_response', ''))
    assert marker in response, 'persisted history not recalled'
    db.close()
    result = {'passed': True, 'engine': 'Hermes.AIAgent', 'provider': 'openai-codex',
              'compression_threshold': 0.5, 'source_bytes': len(raw), 'source_lines': 18002,
              'full_file_sha256_verified': True, 'original_request_exact': True,
              'tool_result_count': tool_count, 'history_recalled': True,
              'telegram_adapter_started': False}
    (workspace / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
