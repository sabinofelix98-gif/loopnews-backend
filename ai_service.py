"""AI Service - Credibility analysis and news summarization using OpenAI"""
import re
import json
import os
from typing import Optional
from openai import AsyncOpenAI

# Initialize OpenAI client
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY') or os.environ.get('EMERGENT_LLM_KEY')

async def analyze_credibility(title: str, summary: str, source_name: str) -> Optional[dict]:
    """Use AI to analyze news credibility"""
    if not OPENAI_API_KEY:
        return None
    
    try:
        client = AsyncOpenAI(api_key=OPENAI_API_KEY)
        
        system_message = """Você é um especialista em verificação de notícias (fact-checker).
Analise a notícia fornecida e determine sua credibilidade.

CRITÉRIOS DE AVALIAÇÃO:
- Linguagem sensacionalista ou clickbait
- Afirmações extraordinárias sem evidências
- Teorias da conspiração
- Promessas milagrosas ou impossíveis
- Manipulação emocional excessiva
- Fonte confiável ou desconhecida

Responda APENAS em JSON no formato:
{"score": 0.0 a 1.0, "reason": "breve explicação em português"}

Onde score é:
- 0.0-0.3 = Provavelmente fake news
- 0.4-0.6 = Suspeito, precisa verificação
- 0.7-1.0 = Provavelmente confiável"""
        
        prompt = f"""Analise esta notícia:

TÍTULO: {title}
RESUMO: {summary}
FONTE: {source_name or 'Desconhecida'}

Responda apenas o JSON com score e reason."""

        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": prompt}
            ],
            max_tokens=200,
            temperature=0.3
        )
        
        result_text = response.choices[0].message.content
        
        try:
            json_match = re.search(r'\{[^}]+\}', result_text)
            if json_match:
                result = json.loads(json_match.group())
                if "score" in result:
                    return result
        except json.JSONDecodeError:
            pass
        
        return None
    except Exception as e:
        print(f"AI credibility analysis error: {str(e)}")
        return None


async def generate_summary(title: str, content: str, category: str) -> Optional[str]:
    """Use AI to generate a concise Portuguese summary of a news article"""
    if not OPENAI_API_KEY:
        return None
    
    if not content or len(content.strip()) < 50:
        return None
    
    try:
        client = AsyncOpenAI(api_key=OPENAI_API_KEY)
        
        system_message = """Você é um jornalista brasileiro experiente e revisor de textos.
Sua tarefa é criar um resumo conciso e informativo de notícias.

REGRAS OBRIGATÓRIAS:
- Máximo 2 frases (50-80 palavras)
- Português brasileiro PERFEITO: gramática, acentuação e ortografia impecáveis
- Linguagem clara, direta e profissional
- Capture o fato principal e o impacto da notícia
- NÃO use aspas, citações diretas ou emojis
- NÃO comece com "A notícia", "O artigo", "Segundo" ou "De acordo"
- Comece direto com o fato principal (sujeito + verbo)
- Tom informativo e neutro, sem sensacionalismo
- Revise a ortografia antes de responder"""
        
        truncated = content[:1500] if len(content) > 1500 else content
        
        prompt = f"""Resuma esta notícia em 2 frases curtas e bem escritas em português brasileiro:

TÍTULO: {title}
CATEGORIA: {category}
CONTEÚDO: {truncated}"""

        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": prompt}
            ],
            max_tokens=200,
            temperature=0.5
        )
        
        result_text = response.choices[0].message.content
        
        if result_text and len(result_text.strip()) > 20:
            summary = result_text.strip()
            summary = re.sub(r'^["\']+|["\']+$', '', summary)
            summary = re.sub(r'\n+', ' ', summary)
            summary = re.sub(r'\s+', ' ', summary).strip()
            return summary
        
        return None
    except Exception as e:
        print(f"AI summary generation error: {str(e)}")
        return None
