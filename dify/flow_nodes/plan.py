import json, re, hashlib, copy

HELP='''欢迎来到星际失物招领局。柜台只接三种抽象失物：周末、勇气、笑话。

每条指令前写一个新的动作编号，同一步重试沿用原编号：
1. `@a1 登记 周末 | 想把周末留给自己`（也可登记勇气或笑话）
2. 读完招领员给出的两个选项，发送 `@a2 选A | 我的选择理由` 或 `@a2 选B`
3. 发送 `@a3 领取`，拿到回执即完成一份委托。
4. 下次用同一玩家代号打开新会话，发送 `@a4 再来`，招领员会读出你实际留下的档案，再开始新委托。

理由可省略，限160字。玩家代号只是本机档案标签，不是登录凭据；请用虚构代号。'''

def main(query:str,player:str,state_body:str,status_code:float)->dict:
    def stop(message): return {'route':'answer','message':message,'prompt':'','plan_json':''}
    if not re.fullmatch(r'[A-Za-z0-9_-]{3,24}',player or ''):
        return stop('玩家代号需为3–24位英文字母、数字、下划线或短横线。请在新会话设置有效代号。')
    if int(status_code)!=200:
        return stop('档案读取失败，未调用模型、未推进委托。请稍后用同一编号重试；若持续失败，检查状态服务。')
    try: saved=json.loads(state_body)
    except (ValueError,TypeError): return stop('档案格式异常，未推进委托。请检查配套状态服务。')
    if saved.get('player')!=player: return stop('档案身份不匹配，未推进委托。')
    raw=(query or '').strip()
    if raw in ('','开始','帮助','怎么玩'): return stop(HELP)
    if len(raw)>300: return stop('指令过长，未推进委托。请将理由缩短至160字。\n\n'+HELP)
    match=re.fullmatch(r'@([A-Za-z0-9_-]{1,32})\s+(登记\s+(?:周末|勇气|笑话)|选A|选B|领取|再来)(?:\s*[|｜]\s*([^\n]{0,160}))?',raw)
    if not match: return stop('这张委托单的格式不对，未推进委托。\n\n'+HELP)
    action_id,action,reason=match.groups();reason=(reason or '').strip()
    action=re.sub(r'\s+',' ',action)
    fingerprint=hashlib.sha256(json.dumps({'player':player,'action':action,'reason':reason},ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    previous=saved.get('actions',{}).get(action_id)
    if previous:
        if previous['request_hash']!=fingerprint:
            return stop('这个动作编号已用于另一条指令，未推进委托。重试原指令请保留编号；新动作请换一个编号。')
        return stop(previous['reply']+'\n\n已保存（本次为重试，未重复推进） · 修订 '+str(previous['revision'])+' · 来源 `'+previous['source_id']+'`')
    if saved.get('revision',0)>=500: return stop('本玩家已达到500个动作上限。请使用新代号，或按运行说明仅重置本题样例数据。')
    state=copy.deepcopy(saved.get('state',{'stage':0,'completed':[],'visits':0}))
    stage=state.get('stage',0);completed=state.get('completed',[])
    new=copy.deepcopy(state)
    lost=state.get('lost','');choice=state.get('choice','')
    souvenirs={'周末':{'A':'装着星期八的口袋票','B':'可分给朋友的半日星图'},'勇气':{'A':'会发烫的出发扣','B':'写着一起走的双人徽章'},'笑话':{'A':'晚三秒响的笑声罐','B':'能接住冷场的纸月亮'}}
    if action.startswith('登记 '):
        if stage!=0: return stop('当前委托还没结束。'+('请选A或选B。' if stage==1 else '请发送 `@新编号 领取`。' if stage==2 else '已领取，请先发送 `@新编号 再来`。'))
        lost=action.split(' ',1)[1]
        new.update(stage=1,lost=lost,commission_id=action_id,reason=reason)
        header='### 第一幕 · 登记失物'
        instruction='请选择处理方式：\n\n**A 原样找回**：把失物留给自己，领取一件个人纪念物。\n\n**B 换种方式**：将失物改装成可以与别人分享的纪念物。\n\n发送 `@新编号 选A | 理由` 或 `@新编号 选B | 理由`。'
    elif action in ('选A','选B'):
        if stage!=1: return stop('现在不能选择。'+('先登记一种失物。' if stage==0 else '选择已记下，请领取回执。' if stage==2 else '委托已结束，请发送再来。'))
        choice=action[-1];new.update(stage=2,choice=choice,choice_reason=reason,souvenir=souvenirs[lost][choice])
        header='### 第二幕 · '+('原样找回' if choice=='A' else '换种方式')
        instruction='已选 **'+choice+'**。接下来将领取 **'+new['souvenir']+'**。\n\n发送 `@新编号 领取` 完成委托。'
    elif action=='领取':
        if stage!=2: return stop('现在还不能领取。'+('先登记失物，再选A或选B。' if stage in (0,1) else '这份委托已领取；使用原编号可查看原回执，或用新编号发送再来。'))
        new['stage']=3
        new['completed']=completed+[{'commission_id':state['commission_id'],'lost':lost,'choice':choice,'souvenir':state['souvenir'],'reason':state.get('choice_reason','')}]
        header='### 第三幕 · 招领回执'
        instruction='**玩家代号**：'+player+'\n\n**失物**：'+lost+'\n\n**处理方式**：'+choice+'（'+('原样找回' if choice=='A' else '换种方式')+'）\n\n**纪念物**：'+state['souvenir']+'\n\n本次委托完成。下次用同一玩家代号开启新会话，发送 `@新编号 再来`。'
    elif action=='再来':
        if stage!=3: return stop('请先完成当前委托的登记、选择和领取，再回来开启新委托。')
        new={'stage':0,'completed':completed,'visits':state.get('visits',0)+1}
        header='### 回访 · 柜台又亮了'
        instruction='可以登记下一份委托了：`@新编号 登记 勇气 | 理由`（也可选周末或笑话）。'
    else: return stop(HELP)
    if completed:
        last=completed[-1]
        memory='档案里实际记着：你上次丢失了“'+last['lost']+'”，选择 '+last['choice']+'，带走“'+last['souvenir']+'”；已完成 '+str(len(completed))+' 份委托。'
    else: memory='档案里还没有已完成的委托；招领员不会假装以前认识你。'
    source=saved.get('last_source')
    if source: memory+='（读取来源 '+source['source_id']+'，'+source['at']+'）'
    prompt=json.dumps({'player':player,'action':action,'reason':reason,'lost':lost,'choice':choice,
                       'souvenir':new.get('souvenir',state.get('souvenir','')),'memory':memory,'target_stage':new['stage']},ensure_ascii=False)
    plan={'player':player,'action_id':action_id,'expected_revision':saved['revision'],'request_hash':fingerprint,
          'state':new,'action':action,'input_text':raw,'header':header,'instruction':instruction,'memory':memory}
    return {'route':'generate','message':'','prompt':prompt,'plan_json':json.dumps(plan,ensure_ascii=False)}
