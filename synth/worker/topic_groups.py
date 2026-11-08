"""Reconcile window-local outlines without dropping or inventing source turns."""
import json

POLICY = 'global_outline_complete_partition_v1'


def validate_groups(data, topics, by_id):
    if not isinstance(data,dict) or set(data)!={'groups'} or not isinstance(data['groups'],list):
        raise ValueError('topic_groups_schema')
    if not 1 <= len(data['groups']) <= min(8,len(topics)):
        raise ValueError('topic_groups_count')
    seen,result=set(),[]
    order={identifier:i for i,identifier in enumerate(by_id)}
    for group in data['groups']:
        if not isinstance(group,dict) or set(group)!={'title','indices'}:
            raise ValueError('topic_groups_item')
        title,indices=group['title'],group['indices']
        if not isinstance(title,str) or not title.strip() or len(title)>200 or not isinstance(indices,list) or not indices:
            raise ValueError('topic_groups_item')
        ids=[]
        for index in indices:
            if type(index) is not int or not 0<=index<len(topics) or index in seen:
                raise ValueError('topic_groups_partition')
            seen.add(index);ids.extend(topics[index]['ids'])
        ids.sort(key=order.__getitem__)
        if sum(len(by_id[i]['line']) for i in ids)>32000:
            raise ValueError('topic_group_too_large')
        result.append({'title':title.strip(),'ids':ids})
    if seen!=set(range(len(topics))):
        raise ValueError('topic_groups_incomplete')
    return result


def group_outline(complete,context,topics,by_id,ask,rules,responses):
    if len(topics)<6:
        return topics
    candidates=[{'index':i,'title':t['title'],'beginning':by_id[t['ids'][0]]['text'][:400],
                 'ending':by_id[t['ids'][-1]]['text'][-400:]} for i,t in enumerate(topics)]
    prompt=(context.header()+'\n\nEstos temas vienen de bloques consecutivos de una misma reunión. '
        'Agrupa las continuaciones del mismo asunto para obtener un índice global legible de 3 a 8 temas, '
        'sin imponer un número fijo. Todos los índices deben aparecer exactamente una vez. '
        'No mezcles asuntos distintos para reducir el número, no inventes decisiones en los títulos y '
        'conserva todos los tramos, incluidos aclaraciones y cierre. Esquema: '
        '{"groups":[{"title":"tema concreto","indices":[0,1]}]}.\n\n'+json.dumps(candidates,ensure_ascii=False))
    return ask(complete,rules,prompt,lambda data:validate_groups(data,topics,by_id),responses,2048)
