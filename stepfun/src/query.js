// General Chinese query cleanup. No document IDs, relevance labels or per-query answers.
export const QUERY_RULE='zh-lexical-expansion-v2';
const aliases={'开会':'会议','账号':'账户','上班':'通勤','汇报':'汇报 幻灯片','测试':'测试 实验 测量','密钥':'密钥 凭据','需求':'需求 规格 上下文','量化':'量化 精度','检索':'检索 查询'};
export function contentQuery(text){
 let value=text.replace(/为什么|怎么样|怎么|怎样|如何|为何|应该|之后|以后|已经|避免|防止|保证|别人|大家|自己|拿到|也能|还能|前后|越来越|太多|多次|什么|不到/g,' ').replace(/[？?，,。！!：:；;的了被又让还吗呢]/g,' ');
 for(const [word,replacement] of Object.entries(aliases))value=value.replaceAll(word,replacement);
 if(text.includes('项目')&&text.includes('运行'))value+=' 环境 依赖 复现';
 return value.replace(/\s+/g,' ').trim()||text.trim();
}
