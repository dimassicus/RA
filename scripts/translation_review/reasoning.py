"""Reasoning-capable LM Studio transport with strict final-answer validation."""
import json
import re
from .lmstudio import local_request
from .reviewer import PROMPT, SCHEMA, ReviewError, validate_response

# Identical for the reasoning-on and matched-off runs. Grammar constraints
# interfere with reasoning output in this installed LM Studio/Qwen combination.
SYSTEM = PROMPT.removesuffix(' /no_think') + '\nFinal answer must be a JSON object, without Markdown or surrounding prose. Use this exact JSON schema:\n' + json.dumps(SCHEMA,sort_keys=True)


def parse_final(content):
    if not isinstance(content,str) or not content.strip():
        raise ValueError('Empty final answer; reasoning is not a substitute')
    text=content.strip()
    wrapper=re.fullmatch(r'```(?:json)?\s*\n(.*?)\n```',text,re.S)
    if wrapper:
        text=wrapper.group(1).strip()
    return json.loads(text), bool(wrapper)


def review(source, source_lang, target_lang, candidates, model, port, *, enabled):
    if not source.strip() or not candidates or any(not t.strip() for t in candidates.values()):
        raise ValueError('Empty input')
    payload={'source_language':source_lang,'target_language':target_lang,
             'source':source,'candidates':candidates}
    content=json.dumps(payload,ensure_ascii=False)
    if len(source)>4000 or len((content+SYSTEM).encode())>11500:
        raise ValueError('Input exceeds conservative context budget')
    request={'model':model,'stream':False,'temperature':0,'seed':42,'max_tokens':8192,
             'reasoning_effort':'medium' if enabled else 'none',
             'chat_template_kwargs':{'enable_thinking':enabled,'reasoning_effort':'medium'},
             'messages':[{'role':'system','content':SYSTEM},{'role':'user','content':content}]}
    envelope=local_request(port,'/v1/chat/completions',request,timeout=600)
    try:
        choices=envelope['choices']
        if len(choices)!=1 or choices[0].get('finish_reason')!='stop':
            raise ValueError('Incomplete generation')
        message=choices[0]['message']
        usage=envelope.get('usage',{})
        reasoning_tokens=usage.get('completion_tokens_details',{}).get('reasoning_tokens',0)
        reasoning_chars=len(message.get('reasoning_content') or '')
        if enabled and (reasoning_tokens<=0 or reasoning_chars==0):
            raise ValueError('Reasoning requested but not observed')
        if not enabled and (reasoning_tokens>0 or reasoning_chars>0):
            raise ValueError('Reasoning appeared in off control')
        data,wrapper=parse_final(message.get('content'))
        validate_response(data,source,candidates)
    except (ValueError,KeyError,TypeError) as exc:
        raise ReviewError(str(exc),envelope) from exc
    return data,{'model':envelope.get('model'),'usage':usage,'finish_reason':choices[0]['finish_reason'],
                 'reasoning_enabled':enabled,'reasoning_chars':reasoning_chars,'removed_code_fence':wrapper}
