"""LM Studio loopback adapter; same prompt, schema and validation as Ollama."""
import json
import urllib.request
from .reviewer import PROMPT, SCHEMA, ReviewError, validate_response


def local_request(port, path, body=None, timeout=300):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(f'http://127.0.0.1:{int(port)}{path}',
        data=None if body is None else json.dumps(body).encode(),
        headers={'Content-Type':'application/json'})
    with opener.open(req, timeout=timeout) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError('Oversized local response')
    return json.loads(raw)


def model_identity(model, port):
    data = local_request(port, '/api/v0/models', timeout=10)
    match = next((m for m in data['data'] if m['id'] == model), None)
    if not match or match.get('state') != 'loaded':
        raise ValueError('Requested LM Studio model is not loaded')
    if match.get('loaded_context_length', 0) < 16384:
        raise ValueError('Reviewer requires at least 16384 context tokens')
    return match


def review(source, source_lang, target_lang, candidates, model, port):
    if not source.strip() or not candidates or any(not t.strip() for t in candidates.values()):
        raise ValueError('Empty input')
    content = json.dumps({'source_language':source_lang,'target_language':target_lang,
                         'source':source,'candidates':candidates},ensure_ascii=False)
    if len(source) > 4000 or len((content+PROMPT+json.dumps(SCHEMA)).encode()) > 11500:
        raise ValueError('Input exceeds conservative context budget')
    request = {'model':model, 'stream':False,
        'messages':[{'role':'system','content':PROMPT},{'role':'user','content':content}],
        'response_format':{'type':'json_schema','json_schema':{
            'name':'translation_review','strict':True,'schema':SCHEMA}},
        'temperature':0, 'seed':42, 'max_tokens':4096,
        'reasoning_effort':'none', 'chat_template_kwargs':{'enable_thinking':False}}
    envelope = local_request(port,'/v1/chat/completions',request)
    try:
        choices = envelope['choices']
        if len(choices) != 1 or choices[0].get('finish_reason') != 'stop':
            raise ValueError('Incomplete generation')
        data = json.loads(choices[0]['message']['content'])
        validate_response(data,source,candidates)
    except (ValueError,KeyError,TypeError) as exc:
        raise ReviewError(str(exc),envelope) from exc
    return data, {'model':envelope.get('model'),'usage':envelope.get('usage'),
                  'finish_reason':choices[0]['finish_reason']}
