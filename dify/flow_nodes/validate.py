import json,re

def main(plan_json:str,model_text:str)->dict:
    error='招领员的回信格式不完整，本次没有发送保存请求。请用同一动作编号重试。'
    try:
        plan=json.loads(plan_json)
        text=re.sub(r'<think>.*?</think>','',model_text or '',flags=re.S).strip()
        text=re.sub(r'^```(?:json)?\s*|\s*```$','',text).strip()
        if text.startswith('{'):
            result=json.loads(text)
            reaction=result.get('reaction')
        else:
            reaction=text
        if not isinstance(reaction,str) or not 3<=len(reaction.strip())<=600: raise ValueError('Invalid reaction')
        if '<think' in reaction.lower(): raise ValueError('Unexpected response format')
        if any(ord(ch)<32 and ch not in '\n\t' for ch in reaction): raise ValueError('Unexpected control characters')
        # Branch decisions and saved state come only from the validated plan.
        reply=plan['header']+'\n\n'+reaction.strip()+'\n\n'+plan['memory']+'\n\n'+plan['instruction']
        body={k:plan[k] for k in ('player','action_id','expected_revision','request_hash','state','action','input_text')}
        body['reply']=reply
        return {'route':'commit','commit_body':json.dumps(body,ensure_ascii=False),'message':''}
    except (ValueError,KeyError,TypeError,AttributeError):
        return {'route':'answer','commit_body':'','message':error}
