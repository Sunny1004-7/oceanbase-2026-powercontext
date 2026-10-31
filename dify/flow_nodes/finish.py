import json

def main(body:str,status_code:float)->dict:
    uncertain='保存结果未确认。请用同一动作编号、同一条指令重试；不要换编号，系统会核对是否已保存，避免重复推进。'
    try:
        result=json.loads(body)
        if int(status_code)!=200 or result.get('saved') is not True:
            detail=result.get('detail','')
            return {'answer':str(detail)+'\n\n'+uncertain}
        return {'answer':result['reply']+'\n\n已保存'+('（重试，未重复推进）' if result.get('duplicate') else '')+' · 修订 '+str(result['revision'])+' · 来源 `'+result['source_id']+'`'}
    except (ValueError,TypeError,KeyError): return {'answer':uncertain}
