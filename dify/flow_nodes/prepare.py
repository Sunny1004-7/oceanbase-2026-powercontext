import re

def main(query:str)->dict:
    match=re.match(r'^@([A-Za-z0-9_-]{1,32})\s+', (query or '').strip())
    return {'action_id':match.group(1) if match else ''}
