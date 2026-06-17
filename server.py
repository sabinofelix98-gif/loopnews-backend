from fastapi import FastAPI, APIRouter, HTTPException, Response, Request, Depends
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Optional
import uuid
from datetime import datetime, timezone, timedelta
import httpx
import asyncio
import json
import re
from contextlib import asynccontextmanager
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

import sys
sys.path.insert(0, str(ROOT_DIR))

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# API Keys
NEWS_API_KEY = os.environ.get('NEWS_API_KEY', '')
GNEWS_API_KEY = os.environ.get('GNEWS_API_KEY', '')
EMERGENT_LLM_KEY = os.environ.get('EMERGENT_LLM_KEY', '')

# Scheduler instance
scheduler = AsyncIOScheduler()

# Create a router with the /api prefix (define early so endpoints can use it)
api_router = APIRouter(prefix="/api")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ==================== MODELS ====================

class User(BaseModel):
    user_id: str
    email: str
    name: str
    picture: Optional[str] = None
    interests: List[str] = []
    onboarding_completed: bool = False
    push_token: Optional[str] = None
    notifications_enabled: bool = True
    last_news_check: Optional[datetime] = None  # Track when user last checked news
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class UserSession(BaseModel):
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    session_token: str
    expires_at: datetime
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class News(BaseModel):
    news_id: str = Field(default_factory=lambda: f"news_{uuid.uuid4().hex[:12]}")
    title: str
    summary: str
    ai_summary: Optional[str] = None  # AI-generated summary
    content: Optional[str] = None
    image_url: Optional[str] = None
    video_url: Optional[str] = None
    category: str
    source_name: Optional[str] = None
    source_url: Optional[str] = None
    source_api: str = "newsapi"  # newsapi, gnews, rss
    published_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    likes_count: int = 0
    is_breaking: bool = False  # Notícia inédita/destaque
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    credibility_score: float = 1.0  # 0.0 to 1.0 (1.0 = highly credible)
    is_verified: bool = False  # Whether the news has been verified
    verification_reason: Optional[str] = None  # Reason for the credibility score
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class NewsLike(BaseModel):
    like_id: str = Field(default_factory=lambda: f"like_{uuid.uuid4().hex[:12]}")
    user_id: str
    news_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class SavedNews(BaseModel):
    saved_id: str = Field(default_factory=lambda: f"saved_{uuid.uuid4().hex[:12]}")
    user_id: str
    news_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class PushToken(BaseModel):
    push_token_id: str = Field(default_factory=lambda: f"pt_{uuid.uuid4().hex[:12]}")
    user_id: str
    token: str
    platform: str  # ios, android, web
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class Notification(BaseModel):
    notification_id: str = Field(default_factory=lambda: f"notif_{uuid.uuid4().hex[:12]}")
    user_id: str
    title: str
    body: str
    data: dict = {}
    read: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

# ==================== REQUEST/RESPONSE MODELS ====================

class SessionRequest(BaseModel):
    session_id: str

class InterestsUpdate(BaseModel):
    interests: List[str]

class PrivacyConsentRequest(BaseModel):
    accepted: bool

class PushTokenRequest(BaseModel):
    token: str
    platform: str = "expo"

class NotificationSettingsUpdate(BaseModel):
    enabled: bool

class NewsCreate(BaseModel):
    title: str
    summary: str
    content: Optional[str] = None
    image_url: Optional[str] = None
    video_url: Optional[str] = None
    category: str
    source_name: Optional[str] = None
    source_url: Optional[str] = None

class NewsUpdate(BaseModel):
    title: Optional[str] = None
    summary: Optional[str] = None
    content: Optional[str] = None
    image_url: Optional[str] = None
    video_url: Optional[str] = None
    category: Optional[str] = None
    source_name: Optional[str] = None
    source_url: Optional[str] = None

# ==================== AUTH HELPER ====================

async def get_current_user(request: Request) -> User:
    """Get current user from session token (cookie or header)"""
    session_token = request.cookies.get("session_token")
    
    if not session_token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            session_token = auth_header[7:]
    
    if not session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    session_doc = await db.user_sessions.find_one(
        {"session_token": session_token},
        {"_id": 0}
    )
    
    if not session_doc:
        raise HTTPException(status_code=401, detail="Invalid session")
    
    expires_at = session_doc["expires_at"]
    if isinstance(expires_at, str):
        expires_at = datetime.fromisoformat(expires_at)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="Session expired")
    
    user_doc = await db.users.find_one(
        {"user_id": session_doc["user_id"]},
        {"_id": 0}
    )
    
    if not user_doc:
        raise HTTPException(status_code=401, detail="User not found")
    
    return User(**user_doc)

# ==================== AUTH ENDPOINTS ====================

class GoogleAuthRequest(BaseModel):
    email: str
    name: str
    picture: Optional[str] = None
    google_id: str

@api_router.post("/auth/google")
async def google_auth(request: GoogleAuthRequest, response: Response):
    """Authenticate user with Google credentials (for standalone app)"""
    try:
        # Check if user exists
        existing_user = await db.users.find_one(
            {"email": request.email},
            {"_id": 0}
        )
        
        if existing_user:
            user_id = existing_user["user_id"]
            # Update user data
            await db.users.update_one(
                {"user_id": user_id},
                {"$set": {
                    "name": request.name,
                    "picture": request.picture or ""
                }}
            )
            user_doc = await db.users.find_one({"user_id": user_id}, {"_id": 0})
        else:
            # Create new user
            user_id = f"user_{uuid.uuid4().hex[:12]}"
            new_user = User(
                user_id=user_id,
                email=request.email,
                name=request.name,
                picture=request.picture or "",
                interests=[],
                onboarding_completed=False
            )
            await db.users.insert_one(new_user.dict())
            user_doc = new_user.dict()
        
        # Create session
        session_token = f"session_{uuid.uuid4().hex}"
        expires_at = datetime.now(timezone.utc) + timedelta(days=30)
        
        session = UserSession(
            user_id=user_id,
            session_token=session_token,
            expires_at=expires_at
        )
        await db.user_sessions.insert_one(session.dict())
        
        # Set cookie
        response.set_cookie(
            key="session_token",
            value=session_token,
            httponly=True,
            secure=True,
            samesite="none",
            path="/",
            max_age=30 * 24 * 60 * 60
        )
        
        logger.info(f"User authenticated via Google: {request.email}")
        
        return {
            "user": user_doc,
            "session_token": session_token
        }
        
    except Exception as e:
        logger.error(f"Google auth error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@api_router.post("/auth/session")
async def exchange_session(request: SessionRequest, response: Response):
    """Exchange Emergent session_id for app session_token"""
    try:
        async with httpx.AsyncClient() as http_client:
            resp = await http_client.get(
                "https://demobackend.emergentagent.com/auth/v1/env/oauth/session-data",
                headers={"X-Session-ID": request.session_id}
            )
            
            if resp.status_code != 200:
                logger.error(f"Emergent auth error: {resp.status_code} - {resp.text}")
                raise HTTPException(status_code=401, detail="Invalid session")
            
            auth_data = resp.json()
            
            # Check if user exists
            existing_user = await db.users.find_one(
                {"email": auth_data["email"]},
                {"_id": 0}
            )
            
            if existing_user:
                user_id = existing_user["user_id"]
                # Update user data
                await db.users.update_one(
                    {"user_id": user_id},
                    {"$set": {
                        "name": auth_data["name"],
                        "picture": auth_data.get("picture", "")
                    }}
                )
                user_doc = await db.users.find_one({"user_id": user_id}, {"_id": 0})
            else:
                # Create new user
                user_id = f"user_{uuid.uuid4().hex[:12]}"
                new_user = User(
                    user_id=user_id,
                    email=auth_data["email"],
                    name=auth_data["name"],
                    picture=auth_data.get("picture", ""),
                    interests=[],
                    onboarding_completed=False
                )
                await db.users.insert_one(new_user.dict())
                user_doc = new_user.dict()
            
            # Create session
            session_token = f"session_{uuid.uuid4().hex}"
            expires_at = datetime.now(timezone.utc) + timedelta(days=7)
            
            session = UserSession(
                user_id=user_id,
                session_token=session_token,
                expires_at=expires_at
            )
            await db.user_sessions.insert_one(session.dict())
            
            # Set cookie
            response.set_cookie(
                key="session_token",
                value=session_token,
                httponly=True,
                secure=True,
                samesite="none",
                path="/",
                max_age=7 * 24 * 60 * 60
            )
            
            return {
                "user": user_doc,
                "session_token": session_token
            }
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Auth error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@api_router.get("/auth/me")
async def get_me(user: User = Depends(get_current_user)):
    """Get current authenticated user"""
    return user.dict()

@api_router.post("/auth/logout")
async def logout(request: Request, response: Response):
    """Logout user and clear session"""
    session_token = request.cookies.get("session_token")
    
    if session_token:
        await db.user_sessions.delete_one({"session_token": session_token})
    
    response.delete_cookie(
        key="session_token",
        path="/",
        secure=True,
        samesite="none"
    )
    
    return {"message": "Logged out successfully"}

# ==================== USER ENDPOINTS ====================

@api_router.put("/users/interests")
async def update_interests(
    interests_data: InterestsUpdate,
    user: User = Depends(get_current_user)
):
    """Update user interests and mark onboarding as completed"""
    await db.users.update_one(
        {"user_id": user.user_id},
        {"$set": {
            "interests": interests_data.interests,
            "onboarding_completed": True
        }}
    )
    
    updated_user = await db.users.find_one(
        {"user_id": user.user_id},
        {"_id": 0}
    )
    
    return updated_user

@api_router.post("/users/privacy-consent")
async def update_privacy_consent(
    consent_data: PrivacyConsentRequest,
    user: User = Depends(get_current_user)
):
    """Record user's privacy policy consent"""
    consent_record = {
        "accepted": consent_data.accepted,
        "date": datetime.now(timezone.utc).isoformat(),
        "ip": None,
        "version": "1.0"
    }
    
    await db.users.update_one(
        {"user_id": user.user_id},
        {"$set": {"privacy_policy_accepted": consent_record}}
    )
    
    updated_user = await db.users.find_one(
        {"user_id": user.user_id},
        {"_id": 0}
    )
    
    return updated_user

@api_router.get("/privacy-policy")
async def get_privacy_policy():
    """Get the privacy policy text (public endpoint, no auth required)"""
    # Check if there's a custom policy in the database
    custom_policy = await db.app_config.find_one(
        {"config_type": "privacy_policy"},
        {"_id": 0}
    )
    
    if custom_policy and custom_policy.get("content"):
        return {
            "version": custom_policy.get("version", "1.0"),
            "updated_at": custom_policy.get("updated_at", datetime.now(timezone.utc).isoformat()),
            "content": custom_policy["content"]
        }
    
    # Default LGPD-compliant privacy policy
    policy_content = """
# DOCUMENTO JURÍDICO INTEGRAL - LOOPNEWS

---

## PARTE 1: POLÍTICA DE PRIVACIDADE

**Versão 1.0 — Atualizada em Maio de 2026**

O aplicativo **LoopNews** ("nós", "nosso" ou "aplicativo") respeita a sua privacidade e está comprometido com a proteção dos seus dados pessoais, em conformidade com a **Lei Geral de Proteção de Dados (LGPD — Lei nº 13.709/2018)**.

---

## 1. Dados que Coletamos

### 1.1 Dados fornecidos por você:
- **Dados de autenticação:** Nome, endereço de e-mail e foto de perfil, obtidos através do login com Google.
- **Preferências:** Categorias de interesse selecionadas durante o onboarding.

### 1.2 Dados coletados automaticamente:
- **Dados de uso:** Notícias visualizadas, curtidas e salvas.
- **Tokens de notificação:** Para envio de notificações push (quando autorizado).
- **Dados técnicos:** Tipo de dispositivo e sistema operacional.

---

## 2. Como Usamos seus Dados

Utilizamos os dados coletados para:
- Personalizar o feed de notícias com base nos seus interesses.
- Enviar notificações sobre notícias relevantes e urgentes.
- Melhorar a experiência do usuário e a qualidade do aplicativo.
- Gerar estatísticas anônimas de uso do aplicativo.

---

## 3. Base Legal para o Tratamento

O tratamento dos seus dados pessoais é realizado com base nas seguintes hipóteses legais da LGPD:
- **Consentimento (Art. 7º, I):** Para a coleta de dados durante o cadastro e o envio de notificações.
- **Legítimo interesse (Art. 7º, IX):** Para melhorar o serviço e garantir a segurança do sistema.
- **Execução de contrato (Art. 7º, V):** Para fornecer as funcionalidades do aplicativo contratado.

---

## 4. Compartilhamento de Dados

**Não vendemos seus dados pessoais.** Seus dados podem ser compartilhados apenas com:
- **Google:** Para autenticação via Google Sign-In.
- **Provedores de infraestrutura:** Servidores e banco de dados para o funcionamento do app.
- **Autoridades legais:** Quando estritamente exigido por lei ou ordem judicial.

---

## 5. Armazenamento e Segurança

- Seus dados são armazenados em servidores seguros com criptografia.
- Implementamos medidas técnicas e organizacionais para proteger seus dados contra acesso não autorizado, perda ou destruição.
- Os dados são mantidos apenas pelo tempo necessário para cumprir as finalidades descritas nesta política.

---

## 6. Seus Direitos (LGPD — Art. 18)

Você tem o direito de:
- **Confirmar** a existência de tratamento de dados.
- **Acessar** seus dados pessoais mantidos por nós.
- **Corrigir** dados incompletos, inexatos ou desatualizados.
- **Solicitar a anonimização, bloqueio ou eliminação** de dados desnecessários.
- **Revogar** o seu consentimento a qualquer momento.
- **Solicitar a portabilidade** dos seus dados.
- **Solicitar a eliminação** dos dados tratados com base no seu consentimento.

Para exercer seus direitos, entre em contato pelo e-mail: **loopnews@loopnewsapp.com**

---

## 7. Cookies e Tecnologias Similares

Utilizamos cookies e tokens de sessão para:
- Manter sua sessão ativa no dispositivo.
- Lembrar suas preferências de navegação.
- Melhorar a performance geral do aplicativo.

---

## 8. Notificações Push

- As notificações push são enviadas apenas com o seu consentimento explícito.
- Você pode desativar as notificações a qualquer momento diretamente nas configurações do aplicativo ou do seu dispositivo móvel.

---

## 9. Menores de Idade

O LoopNews não é destinado a menores de 13 anos. Não coletamos intencionalmente dados de crianças. Se tomarmos conhecimento de que coletamos dados de um menor de forma inadvertida, tomaremos medidas imediatas para excluí-los de nossos servidores.

---

## 10. Conteúdo de Terceiros e Inteligência Artificial (IA)

**10.1** O LoopNews atua como um agregador automatizado de links jornalísticos de livre acesso na internet. Os textos exibidos nas telas de rolagem são breves resumos gerados de forma automatizada por Inteligência Artificial (IA), criados exclusivamente para fins de indexação e direcionamento de tráfego de usuários.

**10.2** O selo "Verificada" exibido nas publicações atesta, unicamente, que o link de destino é autêntico e pertence oficialmente ao portal de notícias indicado (ex: G1, CNN, Portal Mie, etc.), não constituindo uma checagem de fatos (fact-checking) independente ou endosso editorial por parte do LoopNews.

**10.3** O LoopNews não se responsabiliza pelas opiniões, veracidade, exatidão ou atualizações dos conteúdos jornalísticos dos portais indexados. A responsabilidade integral pelo conteúdo original permanece com o respectivo veículo de imprensa de origem, cujo link direto e identificação são disponibilizados ao usuário em todas as postagens.

---

## 11. Direitos Autorais e Remoção de Conteúdo (Take-Down)

**11.1** O LoopNews respeita estritamente os direitos autorais e de propriedade intelectual. Todo o tráfego de leitura integral da notícia é direcionado diretamente para as páginas e servidores originais dos respectivos criadores.

**11.2** Caso você seja o representante legal de um portal de notícias indexado e não deseje que seus conteúdos públicos sejam resumidos e referenciados em nossa plataforma de agregação, envie uma solicitação formal de exclusão para o e-mail: **loopnews@loopnewsapp.com**. O conteúdo do domínio indicado será removido e bloqueado de nosso sistema em até 48 horas úteis após a validação da titularidade.

---

## 12. Alterações nesta Política

Podemos atualizar esta política periodicamente para refletir melhorias no app ou mudanças legais. Notificaremos você sobre alterações significativas através do aplicativo. O uso continuado do app após as alterações implica aceitação da nova política.

---

## 13. Contato e Encarregado de Dados (DPO)

Para dúvidas, reclamações ou solicitações relacionadas aos seus dados pessoais ou remoção de conteúdo:
- **E-mail:** loopnews@loopnewsapp.com
- **Encarregado de Dados (DPO):** Equipe LoopNews

---

## 14. Foro

Fica eleito o foro da comarca do domicílio do usuário para dirimir quaisquer questões oriundas desta Política de Privacidade.

---

## PARTE 2: TERMOS E CONDIÇÕES DE USO

**Versão 1.0 — Atualizada em Maio de 2026**

Seja bem-vindo ao LoopNews. Ao acessar ou usar nosso aplicativo, você concorda em cumprir e vincular-se aos seguintes Termos de Uso. Caso não concorde com qualquer termo, você não deve utilizar o aplicativo.

---

## 1. Escopo dos Serviços

O LoopNews disponibiliza uma plataforma mobile de agregação de conteúdo informativo. O aplicativo utiliza algoritmos computacionais e inteligência artificial para rastrear, selecionar e apresentar resumos de reportagens públicas e direcionar os usuários, por meio de links externos, aos portais de notícias de origem.

---

## 2. Cadastro e Acesso

**2.1** Para utilizar os recursos personalizados do aplicativo, o usuário deverá realizar a autenticação por meio de sua conta Google.

**2.2** O usuário é o único responsável por manter a segurança de suas credenciais de acesso, sendo vedado o compartilhamento de sua conta com terceiros.

---

## 3. Propriedade Intelectual e Licença de Uso

**3.1** Todo o design gráfico, código-fonte, marcas, logotipos e a identidade visual do LoopNews pertencem exclusivamente aos seus desenvolvedores.

**3.2** Concede-se ao usuário uma licença limitada, revogável, não exclusiva e intransferível para utilizar o aplicativo unicamente para fins pessoais e não comerciais.

**3.3** Os conteúdos jornalísticos e marcas das fontes indexadas (ex: portais de notícias) permanecem sob propriedade exclusiva de seus respectivos titulares.

---

## 4. Condutas Vedadas ao Usuário

Ao utilizar o aplicativo, é expressamente proibido:
- Realizar engenharia reversa, descompilação ou modificação da estrutura do código do aplicativo.
- Utilizar robôs, spiders, raspadores de dados (scraping) ou qualquer método automatizado para extrair dados ou conteúdos do LoopNews.
- Burlar os mecanismos de segurança ou tentar acessar dados de outros usuários.

---

## 5. Exclusão de Garantias e de Responsabilidade

**5.1** O LoopNews não garante a disponibilidade ininterrupta do serviço, podendo este passar por manutenções técnicas ou sofrer instabilidades de rede.

**5.2** O aplicativo não se responsabiliza pela integridade, veracidade, exatidão ou atualidade do conteúdo jornalístico resumido e indexado de terceiros.

**5.3** Não nos responsabilizamos por eventuais danos causados por vírus ou arquivos nocivos contidos nos sites externos acessados através dos links fornecidos pelo aplicativo.

---

## 6. Disposições Gerais

**6.1** O não exercício de qualquer direito previsto nestes Termos não constituirá renúncia.

**6.2** Se qualquer disposição destes Termos for considerada inválida ou inexequível, as demais disposições permanecerão em pleno vigor.

**6.3** Quaisquer dúvidas contratuais deverão ser encaminhadas ao e-mail oficial de suporte: **loopnews@loopnewsapp.com**.

---

*Ao continuar a navegar e utilizar as ferramentas do LoopNews, você manifesta concordância integral e irrevogável com todos os itens contidos neste documento.*
"""
    
    return {
        "version": "1.0",
        "updated_at": "2026-02-01T00:00:00Z",
        "content": policy_content.strip()
    }

@api_router.get("/privacy-policy/html", response_class=Response)
async def get_privacy_policy_html():
    """Public HTML privacy policy page for Play Store / App Store listing"""
    policy_json = await get_privacy_policy()
    content = policy_json["content"]
    
    # Simple markdown to HTML conversion
    html_content = content
    html_content = re.sub(r'^### (.+)$', r'<h3>\1</h3>', html_content, flags=re.MULTILINE)
    html_content = re.sub(r'^## (.+)$', r'<h2>\1</h2>', html_content, flags=re.MULTILINE)
    html_content = re.sub(r'^# (.+)$', r'<h1>\1</h1>', html_content, flags=re.MULTILINE)
    html_content = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', html_content)
    html_content = re.sub(r'^- (.+)$', r'<li>\1</li>', html_content, flags=re.MULTILINE)
    html_content = re.sub(r'^---$', r'<hr>', html_content, flags=re.MULTILINE)
    html_content = html_content.replace('\n\n', '</p><p>').replace('\n', '<br>')
    
    html_page = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Politica de Privacidade - LoopNews</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; color: #333; line-height: 1.6; background: #fafafa; }}
        h1 {{ color: #1a1a2e; border-bottom: 2px solid #e94560; padding-bottom: 10px; }}
        h2 {{ color: #1a1a2e; margin-top: 30px; }}
        h3 {{ color: #555; }}
        li {{ margin: 5px 0; }}
        hr {{ border: none; border-top: 1px solid #ddd; margin: 20px 0; }}
        strong {{ color: #1a1a2e; }}
        .footer {{ text-align: center; margin-top: 40px; padding: 20px; color: #888; font-size: 14px; }}
    </style>
</head>
<body>
    <p>{html_content}</p>
    <div class="footer">
        <p>&copy; 2026 LoopNews. Todos os direitos reservados.</p>
        <p>Versao {policy_json["version"]} - Atualizado em {policy_json["updated_at"][:10]}</p>
    </div>
</body>
</html>"""
    
    return Response(content=html_page, media_type="text/html")


@api_router.get("/download/loopnews-code.zip")
async def download_code_zip():
    """Download the LoopNews code as a ZIP file"""
    file_path = ROOT_DIR / "static" / "loopnews-code.zip"
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Arquivo não encontrado")
    return FileResponse(
        path=str(file_path),
        filename="loopnews-code.zip",
        media_type="application/zip"
    )


@api_router.get("/download/frontend")
async def download_frontend_zip():
    """Download only the frontend code as a smaller ZIP file"""
    file_path = ROOT_DIR / "static" / "loopnews-frontend.zip"
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Arquivo não encontrado")
    return FileResponse(
        path=str(file_path),
        filename="loopnews-frontend.zip",
        media_type="application/zip"
    )


@api_router.get("/delete-account", response_class=Response)
async def delete_account_page():
    """Public HTML page for account/data deletion request (Play Store requirement)"""
    html = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Excluir Conta - LoopNews</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; color: #333; line-height: 1.6; background: #fafafa; }
        h1 { color: #1a1a2e; border-bottom: 2px solid #e94560; padding-bottom: 10px; }
        .form-group { margin: 20px 0; }
        label { display: block; font-weight: 600; margin-bottom: 5px; }
        input, textarea { width: 100%; padding: 12px; border: 1px solid #ddd; border-radius: 8px; font-size: 16px; box-sizing: border-box; }
        textarea { height: 100px; resize: vertical; }
        button { background: #e94560; color: white; border: none; padding: 14px 28px; border-radius: 8px; font-size: 16px; font-weight: 600; cursor: pointer; width: 100%; margin-top: 10px; }
        button:hover { background: #d63651; }
        .info { background: #f0f4ff; border-left: 4px solid #3B82F6; padding: 15px; border-radius: 0 8px 8px 0; margin: 20px 0; }
        .success { display: none; background: #d4edda; border: 1px solid #c3e6cb; padding: 20px; border-radius: 8px; text-align: center; color: #155724; }
        .footer { text-align: center; margin-top: 40px; padding: 20px; color: #888; font-size: 14px; }
    </style>
</head>
<body>
    <h1>Excluir Conta e Dados - LoopNews</h1>
    
    <div class="info">
        <strong>O que sera excluido:</strong><br>
        - Seus dados pessoais (nome, email, foto de perfil)<br>
        - Suas preferencias de categorias<br>
        - Seu historico de noticias visualizadas<br>
        - Seu token de notificacoes push<br>
        <br>
        <strong>Prazo:</strong> Seus dados serao excluidos em ate 48 horas uteis apos a confirmacao.
    </div>

    <div id="form-container">
        <div class="form-group">
            <label>Email da conta (usado no Google Sign-In):</label>
            <input type="email" id="email" placeholder="seu.email@gmail.com" required>
        </div>
        <div class="form-group">
            <label>Motivo (opcional):</label>
            <textarea id="reason" placeholder="Conte-nos por que deseja excluir sua conta..."></textarea>
        </div>
        <button onclick="submitRequest()">Solicitar Exclusao da Conta</button>
    </div>

    <div class="success" id="success-msg">
        <h2>Solicitacao Enviada!</h2>
        <p>Recebemos seu pedido de exclusao. Seus dados serao removidos em ate 48 horas uteis.</p>
        <p>Voce recebera uma confirmacao no email informado.</p>
    </div>

    <div class="footer">
        <p>Duvidas? Entre em contato: <strong>loopnews@loopnewsapp.com</strong></p>
        <p>&copy; 2026 LoopNews. Todos os direitos reservados.</p>
    </div>

    <script>
    async function submitRequest() {
        const email = document.getElementById('email').value;
        const reason = document.getElementById('reason').value;
        if (!email) { alert('Por favor, informe seu email.'); return; }
        try {
            const resp = await fetch('/api/delete-account/request', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({email, reason})
            });
            if (resp.ok) {
                document.getElementById('form-container').style.display = 'none';
                document.getElementById('success-msg').style.display = 'block';
            } else {
                alert('Erro ao enviar solicitacao. Tente novamente.');
            }
        } catch(e) {
            alert('Erro de conexao. Tente novamente.');
        }
    }
    </script>
</body>
</html>"""
    return Response(content=html, media_type="text/html")

@api_router.post("/delete-account/request")
async def request_account_deletion(request: Request):
    """Process account/data deletion request"""
    body = await request.json()
    email = body.get("email", "").strip()
    reason = body.get("reason", "")
    
    if not email:
        raise HTTPException(status_code=400, detail="Email is required")
    
    # Log the deletion request
    await db.deletion_requests.insert_one({
        "email": email,
        "reason": reason,
        "status": "pending",
        "requested_at": datetime.now(timezone.utc),
    })
    
    # Try to find and delete user data
    user = await db.users.find_one({"email": email})
    if user:
        user_id = user.get("user_id", "")
        # Delete all user data
        await db.users.delete_one({"email": email})
        await db.news_likes.delete_many({"user_id": user_id})
        await db.saved_news.delete_many({"user_id": user_id})
        await db.user_sessions.delete_many({"user_id": user_id})
        await db.push_tokens.delete_many({"user_id": user_id})
        await db.notification_logs.delete_many({"user_id": user_id})
        
        # Update deletion request status
        await db.deletion_requests.update_one(
            {"email": email, "status": "pending"},
            {"$set": {"status": "completed", "completed_at": datetime.now(timezone.utc)}}
        )
        logger.info(f"Account deleted for {email}")
    else:
        await db.deletion_requests.update_one(
            {"email": email, "status": "pending"},
            {"$set": {"status": "not_found"}}
        )
    
    return {"status": "ok", "message": "Deletion request processed"}


@api_router.get("/users/profile")
async def get_profile(user: User = Depends(get_current_user)):
    """Get user profile"""
    # Get saved news count
    saved_count = await db.saved_news.count_documents({"user_id": user.user_id})
    
    # Get liked news count
    liked_count = await db.news_likes.count_documents({"user_id": user.user_id})
    
    # Get unread notifications count
    unread_count = await db.notifications.count_documents({
        "user_id": user.user_id,
        "read": False
    })
    
    return {
        **user.dict(),
        "saved_count": saved_count,
        "liked_count": liked_count,
        "unread_notifications": unread_count
    }

# ==================== PUSH NOTIFICATIONS ENDPOINTS ====================

@api_router.post("/notifications/register")
async def register_push_token(
    token_data: PushTokenRequest,
    user: User = Depends(get_current_user)
):
    """Register push notification token"""
    # Remove old tokens for this user
    await db.push_tokens.delete_many({"user_id": user.user_id})
    
    # Save new token
    push_token = PushToken(
        user_id=user.user_id,
        token=token_data.token,
        platform=token_data.platform
    )
    await db.push_tokens.insert_one(push_token.dict())
    
    # Update user
    await db.users.update_one(
        {"user_id": user.user_id},
        {"$set": {"push_token": token_data.token}}
    )
    
    return {"message": "Push token registered successfully"}

@api_router.put("/notifications/settings")
async def update_notification_settings(
    settings: NotificationSettingsUpdate,
    user: User = Depends(get_current_user)
):
    """Update notification settings"""
    await db.users.update_one(
        {"user_id": user.user_id},
        {"$set": {"notifications_enabled": settings.enabled}}
    )
    
    return {"message": "Settings updated", "enabled": settings.enabled}

@api_router.get("/notifications")
async def get_notifications(
    skip: int = 0,
    limit: int = 20,
    user: User = Depends(get_current_user)
):
    """Get user notifications"""
    notifications = await db.notifications.find(
        {"user_id": user.user_id},
        {"_id": 0}
    ).sort("created_at", -1).skip(skip).limit(limit).to_list(length=limit)
    
    return notifications

@api_router.post("/notifications/{notification_id}/read")
async def mark_notification_read(
    notification_id: str,
    user: User = Depends(get_current_user)
):
    """Mark notification as read"""
    await db.notifications.update_one(
        {"notification_id": notification_id, "user_id": user.user_id},
        {"$set": {"read": True}}
    )
    return {"message": "Notification marked as read"}

@api_router.post("/notifications/read-all")
async def mark_all_notifications_read(user: User = Depends(get_current_user)):
    """Mark all notifications as read"""
    await db.notifications.update_many(
        {"user_id": user.user_id},
        {"$set": {"read": True}}
    )
    return {"message": "All notifications marked as read"}

# ==================== NEWS FETCH FUNCTIONS ====================

CATEGORY_MAPPING = {
    "tecnologia": "technology",
    "esporte": "sports",
    "esportes": "sports",
    "política": "politics",
    "politica": "politics",
    "games": "entertainment",
    "economia": "business",
    "entretenimento": "entertainment",
    "saúde": "health",
    "saude": "health",
    "ciência": "science",
    "ciencia": "science",
    # Novas categorias
    "financas": "business",
    "finanças": "business",
    "investimentos": "business",
    "acoes": "business",
    "ações": "business",
    "bolsa": "business",
    "criptomoedas": "business",
    "crypto": "business",
    "bitcoin": "business",
    "anime": "entertainment",
    "filmes": "entertainment",
    "cinema": "entertainment",
    "novelas": "entertainment",
    "series": "entertainment",
    "séries": "entertainment",
    "ficcao": "entertainment",
    "ficção": "entertainment",
    "mundo": "general",
    "famosos": "entertainment",
    "futebol": "sports",
}

GNEWS_CATEGORY_MAPPING = {
    "tecnologia": "technology",
    "esportes": "sports",
    "politica": "nation",
    "games": "entertainment",
    "economia": "business",
    "entretenimento": "entertainment",
    "saude": "health",
    "ciencia": "science",
    # Novas categorias
    "financas": "business",
    "investimentos": "business",
    "criptomoedas": "business",
    "anime": "entertainment",
    "filmes": "entertainment",
    "novelas": "entertainment",
    "series": "entertainment",
    "mundo": "world",
    "famosos": "entertainment",
    "futebol": "sports",
}

# ==================== FAKE NEWS DETECTION ====================

# Configuration (can be updated via admin API)
CREDIBILITY_CONFIG = {
    "min_credibility_score": 0.4,  # Minimum score to include news
    "trusted_source_boost": 0.15,  # Boost for trusted sources
    "unknown_source_penalty": 0.25,  # Penalty for unknown sources
    "fake_indicator_penalty": 0.12,  # Penalty per fake indicator found
    "suspicious_pattern_penalty": 0.08,  # Penalty per suspicious pattern
    "ai_analysis_threshold_low": 0.35,  # Below this, use AI
    "ai_analysis_threshold_high": 0.75,  # Above this, skip AI
}

# Trusted news sources (score boost)
TRUSTED_SOURCES = [
    "g1", "globo", "folha", "uol", "estadao", "bbc", "reuters", 
    "cnn", "ap news", "afp", "efe", "valor", "exame", "infomoney",
    "band", "sbt", "record", "terra", "r7", "ig", "metrópoles",
    "correio braziliense", "o globo", "jornal nacional", "fantástico",
    "carta capital", "el país", "the guardian", "new york times",
    "washington post", "le monde", "der spiegel", "agência brasil"
]

# Keywords that may indicate fake news (clickbait, sensationalism)
FAKE_NEWS_INDICATORS = [
    # Clickbait clássico
    "você não vai acreditar", "chocante", "bomba", "urgente compartilhe",
    "a mídia não quer que você saiba", "segredo revelado", "cura milagrosa",
    "médicos não querem", "governo esconde", "verdade sobre", "exposto",
    "conspiração", "grande farsa", "mentira da mídia", "não é o que parece",
    "descoberta incrível", "cientistas chocados", "100% comprovado",
    "compartilhe antes que apaguem", "isso muda tudo", "finalmente revelado",
    
    # Manipulação emocional
    "você precisa ver isso", "inacreditável", "impressionante", "absurdo",
    "escândalo", "denúncia bombástica", "revelação exclusiva",
    "ninguém esperava", "todo mundo está falando", "viralizou",
    
    # Saúde e ciência falsa
    "cura natural", "remédio caseiro que", "os médicos odeiam",
    "a indústria farmacêutica", "vacina causa", "tratamento proibido",
    "cientistas escondem", "pesquisa censurada", "a cura que funciona",
    
    # Política e teorias
    "nova ordem mundial", "plano globalista", "deep state",
    "elite mundial", "manipulação das massas", "controle mental",
    "grande reset", "agenda secreta", "golpe em andamento",
    
    # Dinheiro fácil
    "ganhe dinheiro rápido", "fique rico", "método secreto",
    "investimento garantido", "lucro certo", "oportunidade única",
    "renda extra fácil", "trabalhe de casa e ganhe",
    
    # Urgência falsa
    "última chance", "só hoje", "vagas limitadas", "acabe logo",
    "não perca tempo", "aja agora", "oportunidade expira"
]

# Pattern for suspicious content
SUSPICIOUS_PATTERNS = [
    r'!!+',  # Multiple exclamation marks
    r'\?\?+',  # Multiple question marks
    r'[A-Z]{10,}',  # Long uppercase text (shouting)
    r'clique aqui',
    r'saiba mais antes que',
    r'link na bio',
    r'arraste para cima',
    r'\$\$\$+',  # Multiple dollar signs
    r'🚨{2,}',  # Multiple alert emojis
    r'‼️{2,}',  # Multiple double exclamation emojis
    r'(?:kkkk|hahaha|rsrsrs){2,}',  # Excessive laughing
    r'(?:\.{4,})',  # Multiple periods
    r'(?:fonte:\s*(?:whatsapp|zap|telegram|facebook))',  # Social media as source
]

# ==================== IMAGE ENHANCEMENT ====================

import random
import hashlib
from html.parser import HTMLParser

# Category-specific image pools using Unsplash (varied, high-quality)
CATEGORY_IMAGE_POOLS = {
    "tecnologia": [
        "https://images.unsplash.com/photo-1518770660439-4636190af475?w=800",
        "https://images.unsplash.com/photo-1488590528505-98d2b5aba04b?w=800",
        "https://images.unsplash.com/photo-1526374965328-7f61d4dc18c5?w=800",
        "https://images.unsplash.com/photo-1550751827-4bd374c3f58b?w=800",
        "https://images.unsplash.com/photo-1535378917042-10a22c95931a?w=800",
        "https://images.unsplash.com/photo-1504639725590-34d0984388bd?w=800",
        "https://images.unsplash.com/photo-1461749280684-dccba630e2f6?w=800",
        "https://images.unsplash.com/photo-1519389950473-47ba0277781c?w=800",
        "https://images.unsplash.com/photo-1555066931-4365d14bab8c?w=800",
        "https://images.unsplash.com/photo-1581091226825-a6a2a5aee158?w=800",
    ],
    "esportes": [
        "https://images.unsplash.com/photo-1461896836934-bd45ba0c8024?w=800",
        "https://images.unsplash.com/photo-1579952363873-27f3bade9f55?w=800",
        "https://images.unsplash.com/photo-1431324155629-1a6deb1dec8d?w=800",
        "https://images.unsplash.com/photo-1517649763962-0c623066013b?w=800",
        "https://images.unsplash.com/photo-1574629810360-7efbbe195018?w=800",
        "https://images.unsplash.com/photo-1508098682722-e99c43a406b2?w=800",
        "https://images.unsplash.com/photo-1552674605-db6ffd4facb5?w=800",
        "https://images.unsplash.com/photo-1560272564-c83b66b1ad12?w=800",
    ],
    "politica": [
        "https://images.unsplash.com/photo-1529107386315-e1a2ed48a620?w=800",
        "https://images.unsplash.com/photo-1555848962-6e79363ec58f?w=800",
        "https://images.unsplash.com/photo-1541872703-74c5e44368f9?w=800",
        "https://images.unsplash.com/photo-1575320181282-9afab399332c?w=800",
        "https://images.unsplash.com/photo-1523995462485-3d171b5c8fa9?w=800",
        "https://images.unsplash.com/photo-1495020689067-958852a7765e?w=800",
    ],
    "economia": [
        "https://images.unsplash.com/photo-1611974789855-9c2a0a7236a3?w=800",
        "https://images.unsplash.com/photo-1590283603385-17ffb3a7f29f?w=800",
        "https://images.unsplash.com/photo-1460925895917-afdab827c52f?w=800",
        "https://images.unsplash.com/photo-1444653614773-995cb1ef9efa?w=800",
        "https://images.unsplash.com/photo-1526304640581-d334cdbbf45e?w=800",
        "https://images.unsplash.com/photo-1504868584819-f8e8b4b6d7e3?w=800",
    ],
    "financas": [
        "https://images.unsplash.com/photo-1611974789855-9c2a0a7236a3?w=800",
        "https://images.unsplash.com/photo-1590283603385-17ffb3a7f29f?w=800",
        "https://images.unsplash.com/photo-1535320903710-d993d3d77d29?w=800",
        "https://images.unsplash.com/photo-1559526324-4b87b5e36e44?w=800",
        "https://images.unsplash.com/photo-1462206092226-f46025ffe607?w=800",
        "https://images.unsplash.com/photo-1579621970563-ebec7560ff3e?w=800",
        "https://images.unsplash.com/photo-1554224155-6726b3ff858f?w=800",
        "https://images.unsplash.com/photo-1633158829585-23ba8f7c8caf?w=800",
    ],
    "investimentos": [
        "https://images.unsplash.com/photo-1611974789855-9c2a0a7236a3?w=800",
        "https://images.unsplash.com/photo-1642790106117-e829e14a795f?w=800",
        "https://images.unsplash.com/photo-1590283603385-17ffb3a7f29f?w=800",
        "https://images.unsplash.com/photo-1535320903710-d993d3d77d29?w=800",
        "https://images.unsplash.com/photo-1579621970563-ebec7560ff3e?w=800",
        "https://images.unsplash.com/photo-1633158829585-23ba8f7c8caf?w=800",
        "https://images.unsplash.com/photo-1460925895917-afdab827c52f?w=800",
        "https://images.unsplash.com/photo-1559526324-4b87b5e36e44?w=800",
        "https://images.unsplash.com/photo-1444653614773-995cb1ef9efa?w=800",
        "https://images.unsplash.com/photo-1526304640581-d334cdbbf45e?w=800",
    ],
    "criptomoedas": [
        "https://images.unsplash.com/photo-1518546305927-5a555bb7020d?w=800",
        "https://images.unsplash.com/photo-1622630998477-20aa696ecb05?w=800",
        "https://images.unsplash.com/photo-1639762681485-074b7f938ba0?w=800",
        "https://images.unsplash.com/photo-1621761191319-c6fb62004040?w=800",
        "https://images.unsplash.com/photo-1642790551116-18e150f248e3?w=800",
        "https://images.unsplash.com/photo-1640340434855-6084b1f4901c?w=800",
        "https://images.unsplash.com/photo-1625806786037-2af608423424?w=800",
        "https://images.unsplash.com/photo-1644143379190-943cbceaf862?w=800",
        "https://images.unsplash.com/photo-1643488072096-0e20f2a6801f?w=800",
        "https://images.unsplash.com/photo-1641580529558-a96cf6efbc72?w=800",
    ],
    "entretenimento": [
        "https://images.unsplash.com/photo-1603190287605-e6ade32fa852?w=800",
        "https://images.unsplash.com/photo-1470229722913-7c0e2dbbafd3?w=800",
        "https://images.unsplash.com/photo-1514525253161-7a46d19cd819?w=800",
        "https://images.unsplash.com/photo-1493225457124-a3eb161ffa5f?w=800",
        "https://images.unsplash.com/photo-1598387993441-a364f854c3e1?w=800",
        "https://images.unsplash.com/photo-1478147427282-58a87a120781?w=800",
    ],
    "saude": [
        "https://images.unsplash.com/photo-1576091160399-112ba8d25d1d?w=800",
        "https://images.unsplash.com/photo-1505751172876-fa1923c5c528?w=800",
        "https://images.unsplash.com/photo-1532938911079-1b06ac7ceec7?w=800",
        "https://images.unsplash.com/photo-1559757175-5700dde675bc?w=800",
        "https://images.unsplash.com/photo-1579684385127-1ef15d508118?w=800",
        "https://images.unsplash.com/photo-1631049307264-da0ec9d70304?w=800",
    ],
    "ciencia": [
        "https://images.unsplash.com/photo-1507413245164-6160d8298b31?w=800",
        "https://images.unsplash.com/photo-1451187580459-43490279c0fa?w=800",
        "https://images.unsplash.com/photo-1532094349884-543bc11b234d?w=800",
        "https://images.unsplash.com/photo-1462331940025-496dfbfc7564?w=800",
        "https://images.unsplash.com/photo-1636466497217-26a8cbeaf0aa?w=800",
        "https://images.unsplash.com/photo-1628595351029-c2bf17511435?w=800",
    ],
    "games": [
        "https://images.unsplash.com/photo-1542751371-adc38448a05e?w=800",
        "https://images.unsplash.com/photo-1612287230202-1ff1d85d1bdf?w=800",
        "https://images.unsplash.com/photo-1511512578047-dfb367046420?w=800",
        "https://images.unsplash.com/photo-1493711662062-fa541adb3fc8?w=800",
        "https://images.unsplash.com/photo-1538481199705-c710c4e965fc?w=800",
        "https://images.unsplash.com/photo-1552820728-8b83bb6b2b28?w=800",
        "https://images.unsplash.com/photo-1636955816868-fcb881e57954?w=800",
    ],
    "anime": [
        "https://images.unsplash.com/photo-1578632767115-351597cf2477?w=800",
        "https://images.unsplash.com/photo-1613376023733-0a73315d9b06?w=800",
        "https://images.unsplash.com/photo-1607604276583-eef5d076aa5f?w=800",
        "https://images.unsplash.com/photo-1560393464-5c69a73c5770?w=800",
        "https://images.unsplash.com/photo-1558618666-fcd25c85f82e?w=800",
        "https://images.unsplash.com/photo-1601850494422-3cf14624b0b3?w=800",
    ],
    "filmes": [
        "https://images.unsplash.com/photo-1536440136628-849c177e76a1?w=800",
        "https://images.unsplash.com/photo-1489599849927-2ee91cede3ba?w=800",
        "https://images.unsplash.com/photo-1440404653325-ab127d49abc1?w=800",
        "https://images.unsplash.com/photo-1478720568477-152d9b164e26?w=800",
        "https://images.unsplash.com/photo-1524712245354-2c4e5e7121c0?w=800",
        "https://images.unsplash.com/photo-1485846234645-a62644f84728?w=800",
        "https://images.unsplash.com/photo-1595769816263-9b910be24d5f?w=800",
    ],
    "series": [
        "https://images.unsplash.com/photo-1522869635100-9f4c5e86aa37?w=800",
        "https://images.unsplash.com/photo-1574375927938-d5a98e8d7e28?w=800",
        "https://images.unsplash.com/photo-1593359677879-a4bb92f829d1?w=800",
        "https://images.unsplash.com/photo-1611162616475-46b635cb6868?w=800",
        "https://images.unsplash.com/photo-1585647347483-22b66260dfff?w=800",
    ],
    "novelas": [
        "https://images.unsplash.com/photo-1522869635100-9f4c5e86aa37?w=800",
        "https://images.unsplash.com/photo-1593359677879-a4bb92f829d1?w=800",
        "https://images.unsplash.com/photo-1585647347483-22b66260dfff?w=800",
        "https://images.unsplash.com/photo-1611162616475-46b635cb6868?w=800",
    ],
    "geral": [
        "https://images.unsplash.com/photo-1504711434969-e33886168f5c?w=800",
        "https://images.unsplash.com/photo-1495020689067-958852a7765e?w=800",
        "https://images.unsplash.com/photo-1586339949916-3e9457bef6d3?w=800",
        "https://images.unsplash.com/photo-1585829365295-ab7cd400c167?w=800",
        "https://images.unsplash.com/photo-1557992260-ec58e38d363c?w=800",
        "https://images.unsplash.com/photo-1488190211105-8b0e65b80b4e?w=800",
    ],
    "mundo": [
        "https://images.unsplash.com/photo-1451187580459-43490279c0fa?w=800",
        "https://images.unsplash.com/photo-1526778548025-fa2f459cd5c1?w=800",
        "https://images.unsplash.com/photo-1557804506-669a67965ba0?w=800",
        "https://images.unsplash.com/photo-1532375810709-75b1da00537c?w=800",
        "https://images.unsplash.com/photo-1521295121783-8a321d551ad2?w=800",
        "https://images.unsplash.com/photo-1589519160732-57fc498494f8?w=800",
        "https://images.unsplash.com/photo-1477959858617-67f85cf4f1df?w=800",
        "https://images.unsplash.com/photo-1502602898657-3e91760cbb34?w=800",
        "https://images.unsplash.com/photo-1485738422979-f5c462d49f04?w=800",
        "https://images.unsplash.com/photo-1494500764479-0c8f2919a3d8?w=800",
    ],
    "famosos": [
        "https://images.unsplash.com/photo-1522869635100-9f4c5e86aa37?w=800",
        "https://images.unsplash.com/photo-1611162616475-46b635cb6868?w=800",
        "https://images.unsplash.com/photo-1598387993441-a364f854c3e1?w=800",
        "https://images.unsplash.com/photo-1470229722913-7c0e2dbbafd3?w=800",
        "https://images.unsplash.com/photo-1514525253161-7a46d19cd819?w=800",
        "https://images.unsplash.com/photo-1516450360452-9312f5e86fc7?w=800",
        "https://images.unsplash.com/photo-1492684223066-81342ee5ff30?w=800",
        "https://images.unsplash.com/photo-1533174072545-7a4b6ad7a6c3?w=800",
    ],
    "futebol": [
        "https://images.unsplash.com/photo-1579952363873-27f3bade9f55?w=800",
        "https://images.unsplash.com/photo-1574629810360-7efbbe195018?w=800",
        "https://images.unsplash.com/photo-1508098682722-e99c43a406b2?w=800",
        "https://images.unsplash.com/photo-1431324155629-1a6deb1dec8d?w=800",
        "https://images.unsplash.com/photo-1560272564-c83b66b1ad12?w=800",
        "https://images.unsplash.com/photo-1553778263-73a83bab9b0c?w=800",
        "https://images.unsplash.com/photo-1522778119026-d647f0596c20?w=800",
        "https://images.unsplash.com/photo-1529900748604-07564a03e7a6?w=800",
    ],
}

class OGImageParser(HTMLParser):
    """Parse HTML to extract Open Graph image"""
    def __init__(self):
        super().__init__()
        self.og_image = None
        self.twitter_image = None
        self.first_large_img = None

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "meta":
            prop = attrs_dict.get("property", "")
            name = attrs_dict.get("name", "")
            content = attrs_dict.get("content", "")
            if prop == "og:image" and content:
                self.og_image = content
            elif name == "twitter:image" and content:
                self.twitter_image = content
        elif tag == "img" and not self.first_large_img:
            src = attrs_dict.get("src", "")
            width = attrs_dict.get("width", "0")
            try:
                if int(width) >= 300 and src:
                    self.first_large_img = src
            except ValueError:
                pass

async def extract_og_image(url: str) -> Optional[str]:
    """Extract Open Graph image from an article URL"""
    if not url:
        return None
    try:
        async with httpx.AsyncClient() as http_client:
            resp = await http_client.get(
                url,
                timeout=8.0,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; LoopNewsBot/1.0)"}
            )
            if resp.status_code != 200:
                return None

            # Only parse the first 50KB for performance
            html_content = resp.text[:50000]
            parser = OGImageParser()
            parser.feed(html_content)

            return parser.og_image or parser.twitter_image or parser.first_large_img
    except Exception as e:
        logger.debug(f"OG image extraction failed for {url[:60]}: {str(e)}")
        return None

def get_category_fallback_image(category: str, title: str = "") -> str:
    """Get a varied fallback image based on category and title hash"""
    cat = category.lower()
    pool = CATEGORY_IMAGE_POOLS.get(cat, CATEGORY_IMAGE_POOLS["geral"])
    # Use title hash to get consistent but varied selection
    idx = int(hashlib.md5(title.encode()).hexdigest(), 16) % len(pool)
    return pool[idx]

async def validate_image_url(url: str) -> bool:
    """Check if an image URL is valid, loads, and is a real image (not tiny icon)"""
    if not url or len(url) < 10 or url.endswith("/"):
        return False
    # Filter out known bad patterns
    bad_patterns = [
        "1x1", "pixel", "spacer", "blank", "logo", "favicon", "icon",
        "tracking", "beacon", "placeholder", "default_avatar",
        "108x81",  # investing.com tiny thumbnails
        "wp-content/plugins",  # WordPress plugin images
    ]
    url_lower = url.lower()
    if any(p in url_lower for p in bad_patterns):
        return False
    try:
        async with httpx.AsyncClient() as http_client:
            resp = await http_client.head(
                url,
                timeout=5.0,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; LoopNewsBot/1.0)"}
            )
            if resp.status_code != 200:
                return False
            content_type = resp.headers.get("content-type", "")
            if content_type and "image" not in content_type:
                return False
            # Check content-length if available (reject < 5KB as likely icon/placeholder)
            content_length = resp.headers.get("content-length", "0")
            try:
                if int(content_length) < 5000 and int(content_length) > 0:
                    return False
            except ValueError:
                pass
            return True
    except Exception:
        return False

async def enhance_news_image(news_data: dict) -> dict:
    """Enhance news image: validate current, try OG extraction, then category fallback"""
    image_url = news_data.get("image_url", "")
    source_url = news_data.get("source_url", "")

    # Step 1: Always try OG extraction first — these are the most relevant images
    if source_url:
        og_image = await extract_og_image(source_url)
        if og_image and len(og_image) > 10:
            news_data["image_url"] = og_image
            return news_data

    # Step 2: Check if existing image URL is valid
    if image_url and await validate_image_url(image_url):
        return news_data

    # Step 3: Fallback to category-specific varied image
    news_data["image_url"] = get_category_fallback_image(
        news_data.get("category", "geral"),
        news_data.get("title", "")
    )
    return news_data

# Model for filtered (rejected) news
class FilteredNews(BaseModel):
    filtered_id: str = Field(default_factory=lambda: f"filtered_{uuid.uuid4().hex[:12]}")
    title: str
    summary: str
    source_name: Optional[str] = None
    source_url: Optional[str] = None
    source_api: str
    category: str
    credibility_score: float
    rejection_reason: str
    indicators_found: List[str] = []
    filtered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

async def verify_news_credibility(title: str, summary: str, source_name: str) -> dict:
    """
    Verify if news is potentially fake using AI and heuristics.
    Returns: {"credibility_score": float, "is_fake": bool, "reason": str, "indicators": list}
    """
    config = CREDIBILITY_CONFIG
    credibility_score = 1.0
    reasons = []
    indicators_found = []
    
    # 1. Check source credibility
    source_lower = source_name.lower() if source_name else ""
    is_trusted_source = any(trusted in source_lower for trusted in TRUSTED_SOURCES)
    
    if is_trusted_source:
        credibility_score = min(credibility_score + config["trusted_source_boost"], 1.0)
        reasons.append("Fonte confiável")
    elif not source_name:
        credibility_score -= config["unknown_source_penalty"]
        reasons.append("Fonte desconhecida")
        indicators_found.append("fonte_desconhecida")
    
    # 2. Check for clickbait/sensationalist keywords
    text_to_check = f"{title} {summary}".lower()
    
    for indicator in FAKE_NEWS_INDICATORS:
        if indicator in text_to_check:
            indicators_found.append(indicator)
            credibility_score -= config["fake_indicator_penalty"]
    
    if len(indicators_found) > 0:
        reasons.append(f"Linguagem sensacionalista ({len(indicators_found)} indicadores)")
    
    # 3. Check for suspicious patterns
    patterns_found = []
    for pattern in SUSPICIOUS_PATTERNS:
        if re.search(pattern, f"{title} {summary}", re.IGNORECASE):
            patterns_found.append(pattern)
            credibility_score -= config["suspicious_pattern_penalty"]
    
    if patterns_found:
        reasons.append(f"Padrões suspeitos ({len(patterns_found)})")
        indicators_found.extend(patterns_found)
    
    # 4. Use AI for deeper analysis (only if score is borderline)
    if config["ai_analysis_threshold_low"] < credibility_score < config["ai_analysis_threshold_high"] and EMERGENT_LLM_KEY:
        try:
            ai_result = await analyze_news_with_ai(title, summary, source_name)
            if ai_result:
                # Weight AI score with existing score
                credibility_score = (credibility_score * 0.4) + (ai_result["score"] * 0.6)
                if ai_result.get("reason"):
                    reasons.append(f"IA: {ai_result['reason']}")
        except Exception as e:
            logger.error(f"AI analysis error: {str(e)}")
    
    # Ensure score is within bounds
    credibility_score = max(0.0, min(1.0, credibility_score))
    
    return {
        "credibility_score": round(credibility_score, 2),
        "is_fake": credibility_score < config["min_credibility_score"],
        "reason": "; ".join(reasons) if reasons else "Verificação padrão",
        "indicators": indicators_found
    }

async def analyze_news_with_ai(title: str, summary: str, source_name: str) -> Optional[dict]:
    """Use AI to analyze news credibility"""
    if not EMERGENT_LLM_KEY:
        return None
    
    try:
        from emergentintegrations.llm.chat import LlmChat, UserMessage
        
        chat = LlmChat(
            api_key=EMERGENT_LLM_KEY,
            session_id=f"fakenews_{uuid.uuid4().hex[:8]}",
            system_message="""Você é um especialista em verificação de notícias (fact-checker).
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
        ).with_model("openai", "gpt-4.1-mini")
        
        prompt = f"""Analise esta notícia:

TÍTULO: {title}
RESUMO: {summary}
FONTE: {source_name or 'Desconhecida'}

Responda apenas o JSON com score e reason."""

        user_message = UserMessage(text=prompt)
        response = await chat.send_message(user_message)
        
        # Parse JSON from response
        try:
            # Try to extract JSON from response
            json_match = re.search(r'\{[^}]+\}', response)
            if json_match:
                result = json.loads(json_match.group())
                if "score" in result:
                    return result
        except json.JSONDecodeError:
            pass
        
        return None
        
    except Exception as e:
        logger.error(f"AI analysis error: {str(e)}")
        return None

async def should_include_news(title: str, summary: str, source_name: str) -> tuple:
    """
    Determine if news should be included in the feed.
    Returns: (should_include: bool, credibility_data: dict)
    """
    # Quick rejection for obvious fake news patterns
    text_lower = f"{title} {summary}".lower()
    
    # Immediate rejection patterns
    immediate_reject_patterns = [
        "compartilhe antes que apaguem",
        "a verdade que não querem",
        "cura definitiva para",
        "governo esconde a verdade",
        "mídia não mostra isso"
    ]
    
    for pattern in immediate_reject_patterns:
        if pattern in text_lower:
            return False, {
                "credibility_score": 0.0,
                "is_fake": True,
                "reason": "Padrão de fake news detectado automaticamente"
            }
    
    # Full credibility check
    credibility = await verify_news_credibility(title, summary, source_name)
    
    # Include news only if credibility score >= 0.4
    should_include = credibility["credibility_score"] >= 0.4
    
    return should_include, credibility

import html as html_module
from difflib import SequenceMatcher

def clean_news_text(text: str) -> str:
    """Limpa texto de notícias removendo HTML, entities e formatação"""
    if not text:
        return ""
    text = html_module.unescape(text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'\.{2,}', '.', text)
    return text.strip()

def normalize_title(title: str) -> str:
    """Normaliza título para comparação de duplicatas"""
    if not title:
        return ""
    t = title.lower().strip()
    t = re.sub(r'[^\w\s]', '', t)  # remove pontuação
    t = re.sub(r'\s+', ' ', t)
    return t

def titles_are_similar(title1: str, title2: str, threshold: float = 0.85) -> bool:
    """Verifica se dois títulos são similares usando SequenceMatcher"""
    n1 = normalize_title(title1)
    n2 = normalize_title(title2)
    if n1 == n2:
        return True
    return SequenceMatcher(None, n1, n2).ratio() >= threshold

async def is_duplicate_news(title: str, source_url: str = "") -> bool:
    """Verifica se a notícia é duplicata por URL, título exato, prefixo de 6 palavras ou fuzzy"""
    # 1. Check exact URL match
    if source_url:
        url_match = await db.news.find_one({"source_url": source_url})
        if url_match:
            return True
    
    # 2. Check exact title match
    exact = await db.news.find_one({"title": title})
    if exact:
        return True
    
    normalized = normalize_title(title)
    if not normalized or len(normalized) < 15:
        return False
    
    words = normalized.split()
    
    # 3. 6-word prefix match (catches near-identical articles from different sources)
    if len(words) >= 6:
        prefix = " ".join(words[:6])
        prefix_regex = re.escape(prefix)
        candidate = await db.news.find_one(
            {"title": {"$regex": prefix_regex, "$options": "i"}},
            {"_id": 0, "title": 1}
        )
        if candidate and candidate["title"] != title:
            return True
    
    # 4. Fuzzy match via first 2 words (broader search)
    if len(words) >= 3:
        candidates = await db.news.find(
            {"title": {"$regex": re.escape(words[0]), "$options": "i"}},
            {"title": 1, "_id": 0}
        ).sort("published_at", -1).limit(50).to_list(length=50)
        
        for candidate in candidates:
            if titles_are_similar(title, candidate["title"]):
                return True
    
    return False

async def process_and_save_news(news_item: News, news_list: list) -> bool:
    """Process a news item: verify credibility, check urgency, save to DB"""
    # Anti-duplicate system: check by URL, exact title, and similar title
    is_dupe = await is_duplicate_news(news_item.title, news_item.source_url or "")
    if is_dupe:
        return False
    
    # Verify news credibility before adding
    should_include, credibility = await should_include_news(
        news_item.title, 
        news_item.summary, 
        news_item.source_name
    )
    
    if should_include:
        news_data = news_item.dict()
        news_data["title"] = clean_news_text(news_data.get("title", ""))
        news_data["summary"] = clean_news_text(news_data.get("summary", ""))
        news_data["credibility_score"] = credibility["credibility_score"]
        news_data["is_verified"] = True
        news_data["verification_reason"] = credibility["reason"]
        
        # Smart reclassification based on content keywords
        news_data["category"] = smart_reclassify(
            news_data.get("title", ""),
            news_data.get("summary", ""),
            news_data.get("category", "geral"),
            news_data.get("source_name", "")
        )
        
        # Enhance image: try OG extraction or category fallback
        news_data = await enhance_news_image(news_data)
        
        # Generate AI summary for articles with enough content
        try:
            from ai_service import generate_summary
            ai_summary = await generate_summary(
                news_data.get("title", ""),
                news_data.get("summary", ""),
                news_data.get("category", "geral")
            )
            if ai_summary:
                news_data["ai_summary"] = ai_summary
        except Exception as e:
            logger.debug(f"AI summary skipped: {str(e)}")
        
        # Check if it's urgent news and send notification
        if is_urgent_news(news_item.title, news_item.summary):
            news_data["is_breaking"] = True
            logger.info(f"🚨 Urgent news detected: {news_item.title[:50]}...")
            # Send urgent notification (async, don't block)
            asyncio.create_task(notify_urgent_news(news_data))
        
        try:
            await db.news.insert_one(news_data)
        except Exception:
            # Duplicate key error (title unique index) - skip silently
            return False
        news_list.append(news_data)
        return True
    else:
        # Save filtered (rejected) news for admin review
        filtered_news = FilteredNews(
            title=news_item.title,
            summary=news_item.summary,
            source_name=news_item.source_name,
            source_url=news_item.source_url,
            source_api=news_item.source_api,
            category=news_item.category,
            credibility_score=credibility["credibility_score"],
            rejection_reason=credibility["reason"],
            indicators_found=credibility.get("indicators", [])
        )
        await db.filtered_news.insert_one(filtered_news.dict())
        logger.info(f"🚫 Fake news filtered: {news_item.title[:50]}... - Score: {credibility['credibility_score']} - Reason: {credibility['reason']}")
        return False

async def fetch_news_from_newsapi(category: str = None):
    """Fetch news from NewsAPI"""
    if not NEWS_API_KEY:
        return []
    
    try:
        params = {
            "apiKey": NEWS_API_KEY,
            "language": "pt",
            "pageSize": 20,
            "country": "br"
        }
        
        if category:
            api_category = CATEGORY_MAPPING.get(category.lower(), category.lower())
            params["category"] = api_category
        
        url = "https://newsapi.org/v2/top-headlines"
        
        async with httpx.AsyncClient() as http_client:
            resp = await http_client.get(url, params=params, timeout=30.0)
            
            if resp.status_code != 200:
                logger.error(f"NewsAPI error: {resp.status_code} - {resp.text}")
                return []
            
            data = resp.json()
            articles = data.get("articles", [])
            
            news_list = []
            for article in articles:
                if not article.get("title") or article.get("title") == "[Removed]":
                    continue
                
                summary = article.get("description", "") or ""
                if len(summary) > 200:
                    summary = summary[:197] + "..."
                
                news_item = News(
                    title=article.get("title", ""),
                    summary=summary,
                    content=article.get("content", ""),
                    image_url=article.get("urlToImage", ""),
                    category=category or "geral",
                    source_name=article.get("source", {}).get("name", ""),
                    source_url=article.get("url", ""),
                    source_api="newsapi",
                    published_at=datetime.fromisoformat(article.get("publishedAt", "").replace("Z", "+00:00")) if article.get("publishedAt") else datetime.now(timezone.utc)
                )
                
                # Use helper function to process and save news
                await process_and_save_news(news_item, news_list)
            
            return news_list
            
    except Exception as e:
        logger.error(f"Error fetching from NewsAPI: {str(e)}")
        return []

# GNews rate-limit cooldown tracker
_gnews_cooldown_until = None

async def fetch_news_from_gnews(category: str = None):
    """Fetch news from GNews API with rate-limit handling"""
    global _gnews_cooldown_until
    if not GNEWS_API_KEY:
        return []
    
    # Skip if in cooldown period (429 received recently)
    if _gnews_cooldown_until and datetime.now(timezone.utc) < _gnews_cooldown_until:
        logger.debug("GNews: skipping request (rate-limit cooldown)")
        return []
    
    try:
        params = {
            "apikey": GNEWS_API_KEY,
            "lang": "pt",
            "country": "br",
            "max": 10
        }
        
        if category:
            gnews_category = GNEWS_CATEGORY_MAPPING.get(category.lower(), "general")
            params["topic"] = gnews_category
            url = "https://gnews.io/api/v4/top-headlines"
        else:
            url = "https://gnews.io/api/v4/top-headlines"
            params["topic"] = "breaking-news"
        
        async with httpx.AsyncClient() as http_client:
            resp = await http_client.get(url, params=params, timeout=30.0)
            
            if resp.status_code == 429:
                # Set 10-minute cooldown on rate limit
                _gnews_cooldown_until = datetime.now(timezone.utc) + timedelta(minutes=10)
                logger.warning("GNews: rate limited (429). Cooling down for 10 minutes.")
                return []
            
            if resp.status_code != 200:
                logger.error(f"GNews error: {resp.status_code} - {resp.text}")
                return []
            
            data = resp.json()
            articles = data.get("articles", [])
            
            news_list = []
            for article in articles:
                if not article.get("title"):
                    continue
                
                summary = article.get("description", "") or ""
                if len(summary) > 200:
                    summary = summary[:197] + "..."
                
                news_item = News(
                    title=article.get("title", ""),
                    summary=summary,
                    content=article.get("content", ""),
                    image_url=article.get("image", ""),
                    category=category or "geral",
                    source_name=article.get("source", {}).get("name", ""),
                    source_url=article.get("url", ""),
                    source_api="gnews",
                    published_at=datetime.fromisoformat(article.get("publishedAt", "").replace("Z", "+00:00")) if article.get("publishedAt") else datetime.now(timezone.utc)
                )
                
                # Use helper function to process and save news
                await process_and_save_news(news_item, news_list)
            
            return news_list
            
    except Exception as e:
        logger.error(f"Error fetching from GNews: {str(e)}")
        return []

# RSS Feed sources (free, no API key needed)
RSS_FEEDS = [
    # Notícias Gerais
    {"url": "https://g1.globo.com/rss/g1/", "source": "G1", "category": "geral"},
    {"url": "https://g1.globo.com/rss/g1/tecnologia/", "source": "G1 Tech", "category": "tecnologia"},
    {"url": "https://g1.globo.com/rss/g1/economia/", "source": "G1 Economia", "category": "economia"},
    {"url": "https://g1.globo.com/rss/g1/ciencia-e-saude/", "source": "G1 Ciência", "category": "ciencia"},
    {"url": "https://feeds.folha.uol.com.br/esporte/rss091.xml", "source": "Folha Esportes", "category": "esportes"},
    
    # Finanças, Bolsa, Investimentos
    {"url": "https://www.infomoney.com.br/feed/", "source": "InfoMoney", "category": "financas"},
    {"url": "https://exame.com/feed/", "source": "Exame", "category": "financas"},
    {"url": "https://br.investing.com/rss/news.rss", "source": "Investing.com", "category": "investimentos"},
    {"url": "https://www.moneytimes.com.br/feed/", "source": "Money Times", "category": "financas"},
    {"url": "https://einvestidor.estadao.com.br/feed/", "source": "E-Investidor", "category": "investimentos"},
    
    # Criptomoedas
    {"url": "https://portaldobitcoin.uol.com.br/feed/", "source": "Portal do Bitcoin", "category": "criptomoedas"},
    {"url": "https://livecoins.com.br/feed/", "source": "Livecoins", "category": "criptomoedas"},
    {"url": "https://www.criptofacil.com/feed/", "source": "CriptoFácil", "category": "criptomoedas"},
    {"url": "https://cointelegraph.com.br/rss", "source": "CoinTelegraph BR", "category": "criptomoedas"},
    
    # Anime e Cultura Pop
    {"url": "https://www.animenew.com.br/feed/", "source": "AnimeNew", "category": "anime"},
    {"url": "https://www.intoxianime.com/feed/", "source": "IntoxiAnime", "category": "anime"},
    {"url": "https://www.legiaodosherois.com.br/feed/", "source": "Legião dos Heróis", "category": "entretenimento"},
    
    # Games e Cultura Nerd
    {"url": "https://criticalhits.com.br/feed/", "source": "Critical Hits", "category": "games"},
    {"url": "https://jovemnerd.com.br/feed/", "source": "Jovem Nerd", "category": "entretenimento"},
    
    # Filmes e Cinema
    {"url": "https://www.omelete.com.br/rss/noticias.xml", "source": "Omelete", "category": "filmes"},
    {"url": "https://cinepop.com.br/feed/", "source": "CinePOP", "category": "filmes"},
    {"url": "https://www.papelpop.com/feed/", "source": "PapelPop", "category": "filmes"},
    {"url": "https://www.adorocinema.com/rss/", "source": "AdoroCinema", "category": "filmes"},
    
    # Séries e Novelas
    {"url": "https://seriemaniacos.tv/feed/", "source": "Série Maníacos", "category": "series"},
    {"url": "https://tvfoco.com.br/feed/", "source": "TV Foco", "category": "novelas"},
    {"url": "https://rd1.com.br/feed/", "source": "RD1", "category": "novelas"},
    
    # Games
    {"url": "https://www.tecmundo.com.br/rss", "source": "TecMundo", "category": "games"},
    {"url": "https://adrenaline.com.br/rss", "source": "Adrenaline", "category": "games"},
    {"url": "https://www.voxel.com.br/feed/", "source": "Voxel", "category": "games"},
    
    # Notícias Mundiais / Internacionais
    {"url": "https://g1.globo.com/rss/g1/mundo/", "source": "G1 Mundo", "category": "mundo"},
    {"url": "https://www.bbc.com/portuguese/topics/cvjp2jr0k9rt/rss.xml", "source": "BBC Brasil", "category": "mundo"},
    {"url": "https://feeds.folha.uol.com.br/mundo/rss091.xml", "source": "Folha Mundo", "category": "mundo"},
    {"url": "https://rss.uol.com.br/feed/noticias.xml", "source": "UOL Notícias", "category": "mundo"},
    {"url": "https://www.cnnbrasil.com.br/internacional/feed/", "source": "CNN Brasil Internacional", "category": "mundo"},
    {"url": "https://agenciabrasil.ebc.com.br/rss/internacional/feed.xml", "source": "Agência Brasil", "category": "mundo"},
    
    # Famosos, Celebridades, Influencers
    {"url": "https://hugogloss.uol.com.br/feed/", "source": "Hugo Gloss", "category": "famosos"},
    {"url": "https://www.purepeople.com.br/rss", "source": "PurePeople", "category": "famosos"},
    {"url": "https://extra.globo.com/famosos/rss.xml", "source": "Extra Famosos", "category": "famosos"},
    {"url": "https://contigo.uol.com.br/rss.xml", "source": "Contigo!", "category": "famosos"},
    {"url": "https://gshow.globo.com/rss/gshow/", "source": "GShow", "category": "famosos"},
    {"url": "https://www.metropoles.com/famosos-e-entretenimento/feed", "source": "Metrópoles Famosos", "category": "famosos"},
    
    # Futebol — Brasileirão, Transferências, Ligas Europeias
    {"url": "https://ge.globo.com/rss/futebol/", "source": "GE Futebol", "category": "futebol"},
    {"url": "https://ge.globo.com/rss/futebol/futebol-internacional/", "source": "GE Internacional", "category": "futebol"},
    {"url": "https://ge.globo.com/rss/futebol/brasileirao-serie-a/", "source": "GE Brasileirão", "category": "futebol"},
    {"url": "https://ge.globo.com/rss/futebol/times/flamengo/", "source": "GE Flamengo", "category": "futebol"},
    {"url": "https://ge.globo.com/rss/futebol/times/palmeiras/", "source": "GE Palmeiras", "category": "futebol"},
    {"url": "https://ge.globo.com/rss/futebol/times/corinthians/", "source": "GE Corinthians", "category": "futebol"},
    {"url": "https://ge.globo.com/rss/futebol/times/sao-paulo/", "source": "GE São Paulo", "category": "futebol"},
    {"url": "https://www.goal.com/br/feeds/news", "source": "GOAL", "category": "futebol"},
    {"url": "https://www.uol.com.br/esporte/futebol/rss.xml", "source": "UOL Futebol", "category": "futebol"},
    {"url": "https://www.lance.com.br/feed/", "source": "Lance!", "category": "futebol"},
    {"url": "https://90min.com.br/feed", "source": "90min", "category": "futebol"},
    {"url": "https://www.espn.com.br/rss/futebol", "source": "ESPN Futebol", "category": "futebol"},
    {"url": "https://www.cnnbrasil.com.br/esportes/futebol/feed/", "source": "CNN Futebol", "category": "futebol"},
    {"url": "https://www.gazetaesportiva.com/feed/", "source": "Gazeta Esportiva", "category": "futebol"},
    {"url": "https://www.torcedores.com/feed", "source": "Torcedores.com", "category": "futebol"},
]

# ==================== SMART CATEGORY RECLASSIFICATION ====================
CATEGORY_KEYWORDS = {
    "futebol": [
        "futebol", "gol ", "seleção", "copa do mundo", "champions", "libertadores",
        "campeonato brasileiro", "brasileirão", "série a", "série b",
        "premier league", "la liga", "bundesliga", "serie a italiana", "ligue 1",
        "liga dos campeões", "europa league", "copa do brasil", "recopa",
        "jogador", "técnico de futebol", "treinador",
        "flamengo", "palmeiras", "corinthians", "são paulo fc", "vasco",
        "botafogo", "fluminense", "grêmio", "inter de milão", "cruzeiro",
        "atlético", "santos", "bahia", "fortaleza", "ceará",
        "real madrid", "barcelona", "manchester", "liverpool", "arsenal",
        "chelsea", "bayern", "psg", "juventus", "napoli", "milan",
        "borussia", "benfica", "porto fc",
        "transferência", "contratação", "empréstimo", "multa rescisória",
        "escalação", "rodada", "pênalti", "cartão vermelho",
        "copa sulamericana", "libertadores", "recopa",
        "neymar", "mbappé", "haaland", "messi", "cristiano ronaldo",
        "vinicius jr", "endrick", "rodrygo", "raphinha",
    ],
    "esportes": [
        "nba", "nfl", "tênis", "f1", "fórmula 1", "mma", "ufc",
        "olimpíada", "vôlei", "basquete", "natação", "atletismo",
        "boxe", "surfe", "skate", "ciclismo", "maratona",
        "sabalenka", "djokovic", "nadal", "verstappen", "hamilton",
        "lakers", "warriors", "celtics",
    ],
    "famosos": [
        "bbb", "big brother", "paredão", "reality show", "eliminação bbb",
        "celebridade", "famoso", "famosa", "influencer", "influenciador",
        "youtuber", "tiktoker", "instagramer", "streamer",
        "fofoca", "namoro", "casamento", "divórcio", "separação",
        "ator ", "atriz", "cantora", "cantor", "rapper",
        "polêmica", "desabafo", "vídeo viral",
        "instagram", "tiktok", "youtube", "twitter",
        "hugo gloss", "gshow", "fama",
        "horóscopo", "signo",
    ],
    "economia": [
        "pib", "inflação", "selic", "juros", "banco central",
        "dólar", "câmbio", "exportação", "importação",
        "desemprego", "emprego", "clt", "salário mínimo",
        "reforma tributária", "imposto",
        "lotofácil", "loteria", "mega-sena", "quina", "lotomania",
        "resultado da loto", "resultado da mega", "números sorteados",
        "concurso", "prêmio acumulado",
    ],
    "entretenimento": [
        "show ", "festival", "premiação", "oscar", "grammy", "globo de ouro",
        "novela", "receita ", "culinária", "chef ", "cozinha", "restaurante",
        "moda", "beleza", "desfile", "tapete vermelho",
        "música", "álbum", "turnê", "clipe",
    ],
    "filmes": [
        "filme", "cinema", "trailer", "bilheteria", "estreia",
        "diretor ", "roteiro", "elenco", "franquia", "sequência",
        "homem-aranha", "spider-man", "marvel", "dc ", "batman",
        "warner", "disney", "pixar", "netflix", "amazon prime",
        "frankenstein", "horror", "terror",
    ],
    "series": [
        "série ", "temporada", "episódio", "streaming", "apple tv",
        "hbo", "max ", "disney+", "paramount+",
    ],
    "politica": [
        "governo", "presidente", "senado", "câmara", "deputado",
        "senador", "ministro", "congresso", "legislação", "lei ",
        "projeto de lei", "votação", "eleição", "urna", "cpi",
        "impeachment", "stf", "supremo", "tribunal",
        "lula", "bolsonaro", "prefeito", "governador",
        "reforma", "emenda", "orçamento", "plenário",
    ],
    "tecnologia": [
        "inteligência artificial", " ia ", "openai", "google ", "apple ",
        "microsoft", "startup", "app ", "aplicativo", "software",
        "hardware", "chip", "processador", "deepfake", "cibersegurança",
        "hacker", "dados pessoais", "privacidade digital",
    ],
    "criptomoedas": [
        "bitcoin", "ethereum", "btc", "eth", "cripto", "blockchain",
        "token", "nft", "defi", "exchange", "binance", "coinbase",
        "altcoin", "solana", "xrp", "dogecoin", "stablecoin",
    ],
    "saude": [
        "saúde", "médico", "hospital", "doença", "vacina", "pandemia",
        "tratamento", "cirurgia", "medicamento", "farmácia", "sus",
        "diagnóstico", "câncer", "diabetes", "anvisa",
    ],
    "ciencia": [
        "nasa", "espaço", "planeta", "satélite", "pesquisa científica",
        "estudo ", "universidade", "cientista", "descoberta",
        "fóssil", "genética", "dna",
    ],
    "games": [
        "game", "jogo eletrônico", "playstation", "xbox", "nintendo",
        "steam", "pc gamer", "esports", "e-sports", "rpg",
        "fps", "moba", "battle royale",
        "códigos", "código ", "simulador", "roblox", "simulator",
    ],
    "anime": [
        "anime", "mangá", "manga", "otaku", "light novel",
        "shonen", "seinen", "isekai", "crunchyroll",
        "one piece", "naruto", "dragon ball", "demon slayer",
        "jujutsu kaisen", "my hero academia", "attack on titan",
    ],
}

# Source-based forced categories: these sources ALWAYS stay in their category
SOURCE_FORCED_CATEGORY = {
    "G1 Mundo": "mundo",
    "Folha Mundo": "mundo",
    "CNN Brasil Internacional": "mundo",
    "BBC Brasil": "mundo",
    "Agência Brasil": "mundo",
    "GE Futebol": "futebol",
    "GE Internacional": "futebol",
    "GE Brasileirão": "futebol",
    "GE Flamengo": "futebol",
    "GE Palmeiras": "futebol",
    "GE Corinthians": "futebol",
    "GE São Paulo": "futebol",
    "ESPN Futebol": "esportes",
    "CNN Futebol": "futebol",
    "AnimeNew": "anime",
    "IntoxiAnime": "anime",
    "Portal do Bitcoin": "criptomoedas",
    "Livecoins": "criptomoedas",
    "CriptoFácil": "criptomoedas",
    "CoinTelegraph BR": "criptomoedas",
    "Hugo Gloss": "famosos",
    "Extra Famosos": "famosos",
    "GShow": "famosos",
}

def smart_reclassify(title: str, summary: str, current_category: str, source_name: str = "") -> str:
    """Reclassify news based on title, summary, and source keywords with exclusion logic"""
    
    # Check if source has a forced category
    if source_name and source_name in SOURCE_FORCED_CATEGORY:
        return SOURCE_FORCED_CATEGORY[source_name]
    
    text = f"{title} {summary}".lower()
    
    # Exclusion rules: if ANY of these are found, the news CANNOT be in the category
    EXCLUSIONS = {
        "futebol": [
            "nba", "nfl", "tênis", "f1", "fórmula 1", "mma", "ufc", "boxe",
            "vôlei", "basquete", "natação", "surfe", "ciclismo",
            "lutador", "luta livre", "nocaute", "knockout", "cinturão",
            "poatan", "octógono", "peso pesado", "peso leve", "peso galo",
            "bellator", "muay thai", "kickboxing", "cage",
            "verstappen", "hamilton", "norris", "leclerc", "gp de",
            "automobilismo", "grid", "treinos livres", "pit stop",
            "thunder", "spurs", "lakers", "warriors", "celtics", "bucks",
            "indian wells", "grand slam", "roland garros", "wimbledon",
            "vacina", "dengue", "hospital", "doença", "médico", "anvisa",
            "bitcoin", "cripto", "ethereum", "blockchain", "token", "nft",
            "filme", "cinema", "netflix", "disney", "marvel", "série", "temporada",
            "bbb", "big brother", "paredão", "reality",
            "horóscopo", "signo", "lotofácil", "mega-sena",
            "receita ", "culinária", "cozinha",
            "fii", "fiis", "fundo imobiliário", "renda fixa", "tesouro direto",
            "ações ", "ibovespa", "selic", "pix",
        ],
        "investimentos": [
            "futebol", "gol ", "flamengo", "palmeiras", "corinthians", "vasco",
            "botafogo", "fluminense", "campeonato", "brasileirão", "libertadores",
            "champions", "premier league", "la liga",
            "filme", "cinema", "netflix", "disney", "marvel", "série",
            "bbb", "big brother", "paredão",
            "mma", "ufc", "nba", "nfl",
            "vacina", "doença", "hospital",
        ],
        "famosos": [
            "futebol", "gol ", "campeonato", "brasileirão", "libertadores",
            "champions", "premier league",
            "bitcoin", "cripto", "blockchain",
            "vacina", "doença", "hospital", "anvisa",
            "mma", "ufc", "f1", "fórmula 1",
            "lotofácil", "loteria", "mega-sena", "quina", "lotomania",
            "resultado da loto", "resultado da mega", "números sorteados",
            "senado", "câmara", "deputado", "congresso", "PEC", "plenário",
            "relator", "projeto de lei", "votação", "impeachment",
            "stf", "supremo", "tribunal", "julgamento",
        ],
        "esportes": [
            "futebol", "gol ", "campeonato brasileiro", "brasileirão",
            "libertadores", "champions", "premier league", "la liga",
            "flamengo", "palmeiras", "corinthians", "vasco", "botafogo",
            "bbb", "big brother", "paredão",
            "filme", "cinema", "netflix",
            "bitcoin", "cripto",
            "vacina", "doença", "hospital",
        ],
        "financas": [
            "futebol", "gol ", "flamengo", "palmeiras", "corinthians",
            "campeonato", "brasileirão", "libertadores",
            "filme", "cinema", "netflix", "disney", "marvel",
            "bbb", "big brother", "paredão",
            "mma", "ufc", "nba", "nfl",
            "vacina", "doença", "hospital",
        ],
        "mundo": [
            "futebol", "brasileirão", "libertadores", "campeonato brasileiro",
            "bbb", "big brother", "paredão",
            "horóscopo", "signo", "lotofácil", "mega-sena",
            "receita ", "culinária",
        ],
        "anime": [
            "códigos", "código ", "simulador", "simulator", "roblox",
            "futebol", "campeonato", "brasileirão",
            "bitcoin", "cripto", "blockchain",
            "governo", "senado", "deputado", "presidente",
            "jcp", "dividendo", "ibovespa", "selic",
        ],
        "games": [
            "futebol", "campeonato", "brasileirão", "libertadores",
            "bbb", "big brother", "paredão",
            "bitcoin", "cripto", "blockchain",
            "governo", "senado", "deputado", "presidente",
            "jcp", "dividendo", "ibovespa", "selic",
            "vacina", "doença", "hospital",
            "julgamento", "tribunal", "condenado",
        ],
    }
    
    # Score each category (positive matches)
    scores = {}
    for cat, keywords in CATEGORY_KEYWORDS.items():
        score = 0
        for kw in keywords:
            if kw.lower() in text:
                score += 1
        if score > 0:
            scores[cat] = score
    
    if not scores:
        return current_category
    
    # Apply exclusions: remove score from categories that have exclusion matches
    for cat in list(scores.keys()):
        exclusions = EXCLUSIONS.get(cat, [])
        for ex in exclusions:
            if ex.lower() in text:
                del scores[cat]
                break
    
    if not scores:
        return current_category
    
    best_cat = max(scores, key=scores.get)
    best_score = scores[best_cat]
    
    # Only reclassify with high confidence
    generic_sources = ["financas", "investimentos", "economia", "geral", "entretenimento", "esportes"]
    if best_score >= 2 and best_cat != current_category:
        return best_cat
    if best_score >= 1 and current_category in generic_sources and best_cat not in generic_sources:
        return best_cat
    
    return current_category

async def fetch_news_from_rss():
    """Fetch news from RSS feeds"""
    import xml.etree.ElementTree as ET
    from bs4 import BeautifulSoup
    
    all_news = []
    
    async with httpx.AsyncClient() as http_client:
        for feed in RSS_FEEDS:
            try:
                resp = await http_client.get(feed["url"], timeout=15.0,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; LoopNewsBot/1.0)"})
                if resp.status_code != 200:
                    continue
                
                # Try standard XML parser first, fallback to BeautifulSoup for malformed XML
                items = []
                try:
                    root = ET.fromstring(resp.content)
                    items = root.findall(".//item")[:5]
                except ET.ParseError:
                    try:
                        soup = BeautifulSoup(resp.content, "xml")
                        items = soup.find_all("item", limit=5)
                    except Exception:
                        logger.warning(f"Skipping malformed RSS from {feed['source']}")
                        continue
                
                for item in items:
                    try:
                        if hasattr(item, 'find'):
                            title_el = item.find("title")
                            description_el = item.find("description")
                            link_el = item.find("link")
                        else:
                            title_el = item.find("title")
                            description_el = item.find("description")
                            link_el = item.find("link")
                        
                        # Extract text depending on parser type
                        title_text = title_el.text if title_el is not None and title_el.text else (title_el.get_text() if hasattr(title_el, 'get_text') else None)
                        if not title_text:
                            continue
                        
                        desc_text = ""
                        if description_el is not None:
                            desc_text = description_el.text if description_el.text else (description_el.get_text() if hasattr(description_el, 'get_text') else "")
                        
                        link_text = ""
                        if link_el is not None:
                            link_text = link_el.text if link_el.text else (link_el.get_text() if hasattr(link_el, 'get_text') else "")
                        
                        # Try to find image
                        image_url = ""
                        enclosure = item.find("enclosure")
                        if enclosure is not None and enclosure.get("type", "").startswith("image"):
                            image_url = enclosure.get("url", "")
                        
                        media_content = item.find(".//{http://search.yahoo.com/mrss/}content") if hasattr(item, 'findall') else item.find("media:content")
                        if media_content is not None:
                            image_url = media_content.get("url", "")
                        
                        summary = re.sub(r'<[^>]+>', '', desc_text or "")
                        if len(summary) > 200:
                            summary = summary[:197] + "..."
                        
                        news_item = News(
                            title=title_text,
                            summary=summary,
                            image_url=image_url,
                            category=feed["category"],
                            source_name=feed["source"],
                            source_url=link_text,
                            source_api="rss"
                        )
                        
                        await process_and_save_news(news_item, all_news)
                    except Exception as e:
                        logger.debug(f"Skipping item from {feed['source']}: {str(e)}")
                        continue
                        
            except Exception as e:
                logger.error(f"Error fetching RSS from {feed['source']}: {str(e)}")
                continue
    
    return all_news

async def fetch_all_news_sources(category: str = None):
    """Fetch news from all sources concurrently"""
    tasks = [
        fetch_news_from_newsapi(category),
        fetch_news_from_gnews(category),
    ]
    
    # Only fetch RSS for general or if no category
    if not category:
        tasks.append(fetch_news_from_rss())
    
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    all_news = []
    for result in results:
        if isinstance(result, list):
            all_news.extend(result)
    
    return all_news

# ==================== NEWS ENDPOINTS ====================

@api_router.get("/news")
async def get_news(
    tab: str = "general",
    category: Optional[str] = None,
    skip: int = 0,
    limit: int = 20,
    user: User = Depends(get_current_user)
):
    """Get news feed - 'for_you' for personalized, 'general' for all"""
    query = {}
    
    if tab == "for_you" and user.interests:
        query["category"] = {"$in": user.interests}
    
    if category:
        query["category"] = category
    
    # Get user's last check time
    user_doc = await db.users.find_one({"user_id": user.user_id}, {"_id": 0})
    last_check = user_doc.get("last_news_check") if user_doc else None
    
    # Get news from database with stable sort (prevents pagination shift)
    news_cursor = db.news.find(query, {"_id": 0}).sort([
        ("published_at", -1),
        ("news_id", -1)
    ]).skip(skip).limit(limit)
    news_list = await news_cursor.to_list(length=limit)
    
    # Add user-specific data and check if news is "inédita"
    now = datetime.now(timezone.utc)
    
    # Batch check likes and saves (instead of N+1 queries)
    news_ids = [n["news_id"] for n in news_list]
    liked_ids = set()
    saved_ids = set()
    
    liked_docs = await db.news_likes.find(
        {"user_id": user.user_id, "news_id": {"$in": news_ids}},
        {"_id": 0, "news_id": 1}
    ).to_list(length=limit)
    liked_ids = {d["news_id"] for d in liked_docs}
    
    saved_docs = await db.saved_news.find(
        {"user_id": user.user_id, "news_id": {"$in": news_ids}},
        {"_id": 0, "news_id": 1}
    ).to_list(length=limit)
    saved_ids = {d["news_id"] for d in saved_docs}
    
    for news in news_list:
        news["is_liked"] = news["news_id"] in liked_ids
        news["is_saved"] = news["news_id"] in saved_ids
        
        # Mark as "inédita" (new) if:
        # 1. Published in the last 6 hours, OR
        # 2. User hasn't seen it yet (fetched after user's last check)
        published_at = news.get("published_at")
        fetched_at = news.get("fetched_at")
        
        if isinstance(published_at, str):
            published_at = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        if isinstance(fetched_at, str):
            fetched_at = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
        
        is_recent = False
        if published_at:
            if published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=timezone.utc)
            hours_old = (now - published_at).total_seconds() / 3600
            is_recent = hours_old < 6  # Less than 6 hours old
        
        is_new_to_user = False
        if last_check and fetched_at:
            if isinstance(last_check, str):
                last_check = datetime.fromisoformat(last_check.replace("Z", "+00:00"))
            if last_check.tzinfo is None:
                last_check = last_check.replace(tzinfo=timezone.utc)
            if fetched_at.tzinfo is None:
                fetched_at = fetched_at.replace(tzinfo=timezone.utc)
            is_new_to_user = fetched_at > last_check
        
        news["is_breaking"] = is_recent or is_new_to_user or news.get("is_breaking", False)
    
    # Mark trending news - use cached trending data
    try:
        trending_data = await get_cached_trending()
        trending_ids = set()
        for topic in trending_data:
            for article in topic.get("articles", []):
                trending_ids.add(article.get("news_id", ""))
        
        for news in news_list:
            news["is_trending"] = news.get("news_id", "") in trending_ids
    except Exception:
        for news in news_list:
            news["is_trending"] = False
    
    # Update user's last check time (only on first page)
    if skip == 0:
        await db.users.update_one(
            {"user_id": user.user_id},
            {"$set": {"last_news_check": now}}
        )
    
    return news_list

# Trending cache - avoids recomputing on every feed request
_trending_cache = {"data": [], "expires": 0}

async def get_cached_trending(limit: int = 10):
    """Return cached trending data, refresh every 5 minutes"""
    import time
    now = time.time()
    if _trending_cache["data"] and now < _trending_cache["expires"]:
        return _trending_cache["data"][:limit]
    
    data = await get_trending_topics(limit=limit)
    _trending_cache["data"] = data
    _trending_cache["expires"] = now + 300  # 5 minutes
    return data

@api_router.get("/trending")
async def get_trending_topics(limit: int = 8):
    """Get trending topics based on article clustering from last 24-48h (public)"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    
    recent_news = await db.news.find(
        {"published_at": {"$gte": cutoff}},
        {"_id": 0, "news_id": 1, "title": 1, "summary": 1, "ai_summary": 1,
         "image_url": 1, "category": 1, "published_at": 1, "source_name": 1,
         "view_count": 1}
    ).sort("published_at", -1).to_list(500)
    
    if not recent_news:
        return []
    
    # Portuguese stop words to ignore
    STOP_WORDS = {
        "de", "do", "da", "dos", "das", "em", "no", "na", "nos", "nas", "por",
        "para", "com", "sem", "sob", "sobre", "que", "como", "mais", "menos",
        "muito", "seu", "sua", "seus", "suas", "ele", "ela", "eles", "elas",
        "um", "uma", "uns", "umas", "ao", "aos", "às", "é", "são", "foi",
        "ser", "ter", "está", "não", "sim", "já", "mas", "ou", "se", "após",
        "até", "entre", "diz", "pode", "vai", "vão", "tem", "isso", "esse",
        "essa", "este", "esta", "dia", "ano", "anos", "vez", "novo", "nova",
        "novos", "novas", "ainda", "também", "contra", "depois", "antes",
        "durante", "quando", "onde", "bem", "mal", "aqui", "ali", "lá",
        "hoje", "ontem", "amanhã", "agora", "sempre", "nunca", "nesta",
        "neste", "pela", "pelo", "pelos", "pelas", "veja", "saiba",
        "r", "mil", "milhão", "milhões", "bilhão", "bilhões",
        "brasil", "país", "mundo", "casa", "grande", "primeiro", "segunda",
        "feira", "copa", "resultado", "final", "início", "parte", "caso",
        "forma", "acordo", "após", "dois", "três", "quatro", "cinco",
        "janeiro", "fevereiro", "março", "abril", "maio", "junho",
        "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
        "são", "paulo", "rio", "janeiro", "minas", "gerais",
        "confira", "entenda", "assista", "descubra", "conheça",
    }
    
    # Extract significant 2-word bigrams from titles
    from collections import Counter, defaultdict
    bigram_articles = defaultdict(list)
    
    for article in recent_news:
        title = article.get("title", "")
        words = re.sub(r'[^\w\sáàâãéèêíìîóòôõúùûç]', '', title.lower()).split()
        significant_words = [w for w in words if len(w) > 2 and w not in STOP_WORDS]
        
        # Create bigrams
        seen_bigrams = set()
        for i in range(len(significant_words) - 1):
            bigram = f"{significant_words[i]} {significant_words[i+1]}"
            if bigram not in seen_bigrams:
                seen_bigrams.add(bigram)
                bigram_articles[bigram].append(article)
        
        # Also use single significant proper nouns (capitalized in original)
        for word in title.split():
            clean = re.sub(r'[^\w]', '', word)
            if len(clean) > 3 and clean[0].isupper() and clean.lower() not in STOP_WORDS:
                key = clean.lower()
                if key not in seen_bigrams:
                    seen_bigrams.add(key)
                    bigram_articles[key].append(article)
    
    # Score topics: article count * recency bonus
    topic_scores = []
    seen_article_ids = set()
    
    for topic, articles in bigram_articles.items():
        if len(articles) < 2:
            continue
        
        # Deduplicate articles within topic
        unique_articles = []
        for a in articles:
            if a["news_id"] not in seen_article_ids:
                unique_articles.append(a)
        
        if len(unique_articles) < 2:
            continue
        
        # Score = article count + view bonus
        total_views = sum(a.get("view_count", 0) for a in unique_articles)
        score = len(unique_articles) * 10 + total_views
        
        # Find best image
        image = None
        for a in unique_articles:
            if a.get("image_url"):
                image = a["image_url"]
                break
        
        topic_scores.append({
            "topic": topic.title(),
            "article_count": len(unique_articles),
            "score": score,
            "image_url": image,
            "category": unique_articles[0].get("category", "geral"),
            "articles": unique_articles[:3],  # Top 3 sample articles
        })
    
    # Sort by score descending, take top N
    topic_scores.sort(key=lambda x: x["score"], reverse=True)
    
    # Deduplicate overlapping topics (if topic A's articles are subset of B)
    final_topics = []
    used_ids = set()
    for topic in topic_scores:
        article_ids = {a["news_id"] for a in topic["articles"]}
        overlap = len(article_ids & used_ids)
        if overlap < len(article_ids) * 0.5:  # Less than 50% overlap
            used_ids.update(article_ids)
            final_topics.append(topic)
            if len(final_topics) >= limit:
                break
    
    return final_topics

@api_router.post("/news/{news_id}/view")
async def track_news_view(news_id: str):
    """Track a news view (increment view counter)"""
    result = await db.news.update_one(
        {"news_id": news_id},
        {"$inc": {"view_count": 1}}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="News not found")
    return {"status": "ok"}

@api_router.get("/news/new-count")
async def get_new_news_count(user: User = Depends(get_current_user)):
    """Get count of new news since user's last check"""
    user_doc = await db.users.find_one({"user_id": user.user_id}, {"_id": 0})
    last_check = user_doc.get("last_news_check") if user_doc else None
    
    if not last_check:
        # First time user - count news from last 6 hours
        six_hours_ago = datetime.now(timezone.utc) - timedelta(hours=6)
        count = await db.news.count_documents({"published_at": {"$gte": six_hours_ago}})
    else:
        if isinstance(last_check, str):
            last_check = datetime.fromisoformat(last_check.replace("Z", "+00:00"))
        count = await db.news.count_documents({"fetched_at": {"$gt": last_check}})
    
    return {"new_count": count}

@api_router.get("/news/refresh")
async def refresh_news(user: User = Depends(get_current_user)):
    """Force refresh news from all sources"""
    # Count news before refresh
    old_count = await db.news.count_documents({})
    
    await fetch_all_news_sources()
    
    if user.interests:
        for interest in user.interests[:3]:
            await fetch_all_news_sources(category=interest)
    
    # Count new news added
    new_count = await db.news.count_documents({})
    added = new_count - old_count
    
    return {"message": "News refreshed successfully", "new_news_added": added}

@api_router.get("/news/{news_id}")
async def get_single_news(news_id: str, user: User = Depends(get_current_user)):
    """Get a single news item"""
    news = await db.news.find_one({"news_id": news_id}, {"_id": 0})
    
    if not news:
        raise HTTPException(status_code=404, detail="News not found")
    
    news["is_liked"] = await db.news_likes.find_one({
        "user_id": user.user_id,
        "news_id": news_id
    }) is not None
    
    news["is_saved"] = await db.saved_news.find_one({
        "user_id": user.user_id,
        "news_id": news_id
    }) is not None
    
    return news

@api_router.post("/news/{news_id}/like")
async def toggle_like(news_id: str, user: User = Depends(get_current_user)):
    """Toggle like on a news item"""
    existing_like = await db.news_likes.find_one({
        "user_id": user.user_id,
        "news_id": news_id
    })
    
    if existing_like:
        await db.news_likes.delete_one({
            "user_id": user.user_id,
            "news_id": news_id
        })
        # Prevent negative count
        await db.news.update_one(
            {"news_id": news_id, "likes_count": {"$gt": 0}},
            {"$inc": {"likes_count": -1}}
        )
        return {"liked": False}
    else:
        try:
            like = NewsLike(user_id=user.user_id, news_id=news_id)
            await db.news_likes.insert_one(like.dict())
            await db.news.update_one(
                {"news_id": news_id},
                {"$inc": {"likes_count": 1}}
            )
        except Exception:
            pass  # Duplicate like attempt
        return {"liked": True}

@api_router.post("/news/{news_id}/save")
async def toggle_save(news_id: str, user: User = Depends(get_current_user)):
    """Toggle save on a news item"""
    existing_save = await db.saved_news.find_one({
        "user_id": user.user_id,
        "news_id": news_id
    })
    
    if existing_save:
        await db.saved_news.delete_one({
            "user_id": user.user_id,
            "news_id": news_id
        })
        return {"saved": False}
    else:
        try:
            save = SavedNews(user_id=user.user_id, news_id=news_id)
            await db.saved_news.insert_one(save.dict())
        except Exception:
            pass  # Duplicate save attempt
        return {"saved": True}

@api_router.get("/news/saved/list")
async def get_saved_news(
    skip: int = 0,
    limit: int = 20,
    user: User = Depends(get_current_user)
):
    """Get user's saved news"""
    saved_docs = await db.saved_news.find(
        {"user_id": user.user_id},
        {"_id": 0}
    ).sort("created_at", -1).skip(skip).limit(limit).to_list(length=limit)
    
    news_list = []
    for saved in saved_docs:
        news = await db.news.find_one({"news_id": saved["news_id"]}, {"_id": 0})
        if news:
            news["is_liked"] = await db.news_likes.find_one({
                "user_id": user.user_id,
                "news_id": news["news_id"]
            }) is not None
            news["is_saved"] = True
            news_list.append(news)
    
    return news_list

@api_router.get("/news/liked/list")
async def get_liked_news(
    skip: int = 0,
    limit: int = 20,
    user: User = Depends(get_current_user)
):
    """Get user's liked news"""
    liked_docs = await db.news_likes.find(
        {"user_id": user.user_id},
        {"_id": 0}
    ).sort("created_at", -1).skip(skip).limit(limit).to_list(length=limit)
    
    news_list = []
    for liked in liked_docs:
        news = await db.news.find_one({"news_id": liked["news_id"]}, {"_id": 0})
        if news:
            news["is_liked"] = True
            news["is_saved"] = await db.saved_news.find_one({
                "user_id": user.user_id,
                "news_id": news["news_id"]
            }) is not None
            news_list.append(news)
    
    return news_list

# ==================== ADMIN ENDPOINTS ====================

class CredibilityConfigUpdate(BaseModel):
    min_credibility_score: Optional[float] = None
    trusted_source_boost: Optional[float] = None
    unknown_source_penalty: Optional[float] = None
    fake_indicator_penalty: Optional[float] = None
    suspicious_pattern_penalty: Optional[float] = None
    ai_analysis_threshold_low: Optional[float] = None
    ai_analysis_threshold_high: Optional[float] = None

@api_router.get("/admin/dashboard")
async def get_admin_dashboard(user: User = Depends(get_current_user)):
    """Get admin dashboard data"""
    # News statistics
    total_news = await db.news.count_documents({})
    verified_news = await db.news.count_documents({"is_verified": True})
    breaking_news = await db.news.count_documents({"is_breaking": True})
    
    # Filtered news statistics
    total_filtered = await db.filtered_news.count_documents({})
    
    # News by source
    sources_pipeline = [
        {"$group": {"_id": "$source_api", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}}
    ]
    sources = await db.news.aggregate(sources_pipeline).to_list(length=10)
    
    # News by category
    categories_pipeline = [
        {"$group": {"_id": "$category", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}}
    ]
    categories = await db.news.aggregate(categories_pipeline).to_list(length=10)
    
    # Credibility score distribution
    score_distribution = {
        "high": await db.news.count_documents({"credibility_score": {"$gte": 0.8}}),
        "medium": await db.news.count_documents({"credibility_score": {"$gte": 0.5, "$lt": 0.8}}),
        "low": await db.news.count_documents({"credibility_score": {"$gte": 0.4, "$lt": 0.5}}),
    }
    
    # Recent filtered news
    recent_filtered = await db.filtered_news.find(
        {}, {"_id": 0}
    ).sort("filtered_at", -1).limit(10).to_list(length=10)
    
    # User statistics
    total_users = await db.users.count_documents({})
    users_with_notifications = await db.users.count_documents({"notifications_enabled": True})
    
    return {
        "news_stats": {
            "total": total_news,
            "verified": verified_news,
            "breaking": breaking_news,
            "filtered": total_filtered
        },
        "sources": [{"name": s["_id"], "count": s["count"]} for s in sources],
        "categories": [{"name": c["_id"], "count": c["count"]} for c in categories],
        "credibility_distribution": score_distribution,
        "recent_filtered": recent_filtered,
        "user_stats": {
            "total": total_users,
            "with_notifications": users_with_notifications
        },
        "config": CREDIBILITY_CONFIG
    }

@api_router.get("/admin/filtered-news")
async def get_filtered_news(
    skip: int = 0,
    limit: int = 20,
    user: User = Depends(get_current_user)
):
    """Get list of filtered (rejected) news"""
    filtered = await db.filtered_news.find(
        {}, {"_id": 0}
    ).sort("filtered_at", -1).skip(skip).limit(limit).to_list(length=limit)
    
    total = await db.filtered_news.count_documents({})
    
    return {
        "filtered_news": filtered,
        "total": total,
        "skip": skip,
        "limit": limit
    }

@api_router.delete("/admin/filtered-news/{filtered_id}")
async def delete_filtered_news(filtered_id: str, user: User = Depends(get_current_user)):
    """Delete a filtered news record"""
    result = await db.filtered_news.delete_one({"filtered_id": filtered_id})
    
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Filtered news not found")
    
    return {"message": "Filtered news deleted"}

@api_router.post("/admin/filtered-news/{filtered_id}/approve")
async def approve_filtered_news(filtered_id: str, user: User = Depends(get_current_user)):
    """Approve a filtered news (move to main news feed)"""
    filtered = await db.filtered_news.find_one({"filtered_id": filtered_id}, {"_id": 0})
    
    if not filtered:
        raise HTTPException(status_code=404, detail="Filtered news not found")
    
    # Create news from filtered
    news = News(
        title=filtered["title"],
        summary=filtered["summary"],
        source_name=filtered.get("source_name"),
        source_url=filtered.get("source_url"),
        source_api=filtered.get("source_api", "manual"),
        category=filtered.get("category", "geral"),
        credibility_score=1.0,  # Manually approved = high credibility
        is_verified=True,
        verification_reason="Aprovado manualmente pelo admin"
    )
    
    await db.news.insert_one(news.dict())
    await db.filtered_news.delete_one({"filtered_id": filtered_id})
    
    return {"message": "News approved and published", "news_id": news.news_id}

@api_router.get("/admin/credibility-config")
async def get_credibility_config(user: User = Depends(get_current_user)):
    """Get current credibility configuration"""
    return {
        "config": CREDIBILITY_CONFIG,
        "indicators": {
            "fake_news_indicators": len(FAKE_NEWS_INDICATORS),
            "suspicious_patterns": len(SUSPICIOUS_PATTERNS),
            "trusted_sources": len(TRUSTED_SOURCES)
        }
    }

@api_router.put("/admin/credibility-config")
async def update_credibility_config(
    config_update: CredibilityConfigUpdate,
    user: User = Depends(get_current_user)
):
    """Update credibility configuration thresholds"""
    global CREDIBILITY_CONFIG
    
    update_data = {k: v for k, v in config_update.dict().items() if v is not None}
    
    # Validate values
    for key, value in update_data.items():
        if not 0.0 <= value <= 1.0:
            raise HTTPException(status_code=400, detail=f"{key} must be between 0.0 and 1.0")
    
    # Update config
    CREDIBILITY_CONFIG.update(update_data)
    
    # Save to database for persistence
    await db.app_config.update_one(
        {"config_type": "credibility"},
        {"$set": {"values": CREDIBILITY_CONFIG, "updated_at": datetime.now(timezone.utc)}},
        upsert=True
    )
    
    return {"message": "Configuration updated", "config": CREDIBILITY_CONFIG}

@api_router.get("/admin/indicators")
async def get_fake_news_indicators(user: User = Depends(get_current_user)):
    """Get list of fake news indicators and patterns"""
    return {
        "fake_news_indicators": FAKE_NEWS_INDICATORS,
        "suspicious_patterns": SUSPICIOUS_PATTERNS,
        "trusted_sources": TRUSTED_SOURCES
    }

@api_router.post("/admin/news")
async def create_news(news_data: NewsCreate, user: User = Depends(get_current_user)):
    """Create a new news item (admin)"""
    news = News(**news_data.dict(), source_api="manual")
    await db.news.insert_one(news.dict())
    return news.dict()

@api_router.put("/admin/news/{news_id}")
async def update_news(
    news_id: str,
    news_data: NewsUpdate,
    user: User = Depends(get_current_user)
):
    """Update a news item (admin)"""
    update_data = {k: v for k, v in news_data.dict().items() if v is not None}
    
    if not update_data:
        raise HTTPException(status_code=400, detail="No data to update")
    
    result = await db.news.update_one(
        {"news_id": news_id},
        {"$set": update_data}
    )
    
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="News not found")
    
    updated_news = await db.news.find_one({"news_id": news_id}, {"_id": 0})
    return updated_news

@api_router.delete("/admin/news/{news_id}")
async def delete_news(news_id: str, user: User = Depends(get_current_user)):
    """Delete a news item (admin)"""
    result = await db.news.delete_one({"news_id": news_id})
    
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="News not found")
    
    await db.news_likes.delete_many({"news_id": news_id})
    await db.saved_news.delete_many({"news_id": news_id})
    
    return {"message": "News deleted successfully"}

# ==================== CATEGORIES ENDPOINT ====================

@api_router.get("/categories")
async def get_categories():
    """Get available news categories"""
    return [
        # Principais
        {"id": "tecnologia", "name": "Tecnologia", "icon": "laptop"},
        {"id": "esportes", "name": "Esportes", "icon": "football"},
        {"id": "politica", "name": "Política", "icon": "balance-scale"},
        {"id": "games", "name": "Games", "icon": "gamepad"},
        {"id": "economia", "name": "Economia", "icon": "chart-line"},
        {"id": "entretenimento", "name": "Entretenimento", "icon": "film"},
        {"id": "saude", "name": "Saúde", "icon": "heartbeat"},
        {"id": "ciencia", "name": "Ciência", "icon": "flask"},
        # Mundo
        {"id": "mundo", "name": "Mundo", "icon": "globe"},
        # Finanças e Investimentos
        {"id": "financas", "name": "Finanças", "icon": "wallet"},
        {"id": "investimentos", "name": "Investimentos", "icon": "trending-up"},
        {"id": "criptomoedas", "name": "Criptomoedas", "icon": "logo-bitcoin"},
        # Entretenimento Específico
        {"id": "anime", "name": "Anime", "icon": "planet"},
        {"id": "filmes", "name": "Filmes", "icon": "videocam"},
        {"id": "series", "name": "Séries", "icon": "tv"},
        {"id": "novelas", "name": "Novelas", "icon": "heart"},
        # Famosos e Futebol
        {"id": "famosos", "name": "Famosos", "icon": "star"},
        {"id": "futebol", "name": "Futebol", "icon": "football"},
    ]

# ==================== NEWS SOURCES INFO ====================

@api_router.get("/news/sources/info")
async def get_news_sources_info():
    """Get information about news sources"""
    newsapi_count = await db.news.count_documents({"source_api": "newsapi"})
    gnews_count = await db.news.count_documents({"source_api": "gnews"})
    rss_count = await db.news.count_documents({"source_api": "rss"})
    manual_count = await db.news.count_documents({"source_api": "manual"})
    
    return {
        "sources": [
            {"name": "NewsAPI", "count": newsapi_count, "status": "active" if NEWS_API_KEY else "inactive"},
            {"name": "GNews", "count": gnews_count, "status": "active" if GNEWS_API_KEY else "inactive"},
            {"name": "RSS Feeds", "count": rss_count, "status": "active"},
            {"name": "Manual", "count": manual_count, "status": "active"}
        ],
        "total_news": newsapi_count + gnews_count + rss_count + manual_count
    }

# ==================== NOTIFICATION SCHEDULING CONFIG ====================

NOTIFICATION_CONFIG = {
    "auto_notify_enabled": True,
    "min_news_for_notification": 3,  # Minimum new news to trigger notification
    "notify_breaking_immediately": True,  # Send immediate notification for breaking news
    "daily_digest_enabled": True,  # Send daily digest
    "daily_digest_hour": 8,  # Hour to send daily digest (local time)
    "max_notifications_per_day": 10,  # Max notifications per user per day
}

class NotificationScheduleConfig(BaseModel):
    auto_notify_enabled: Optional[bool] = None
    min_news_for_notification: Optional[int] = None
    notify_breaking_immediately: Optional[bool] = None
    daily_digest_enabled: Optional[bool] = None
    daily_digest_hour: Optional[int] = None
    max_notifications_per_day: Optional[int] = None

@api_router.get("/admin/notification-config")
async def get_notification_config(user: User = Depends(get_current_user)):
    """Get notification scheduling configuration"""
    # Get stats
    total_users = await db.users.count_documents({})
    users_with_push = await db.users.count_documents({"push_token": {"$ne": None}})
    users_notifications_on = await db.users.count_documents({"notifications_enabled": True})
    
    # Get today's notification count
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    notifications_today = await db.notifications.count_documents({
        "created_at": {"$gte": today_start}
    })
    
    return {
        "config": NOTIFICATION_CONFIG,
        "stats": {
            "total_users": total_users,
            "users_with_push_token": users_with_push,
            "users_notifications_enabled": users_notifications_on,
            "notifications_sent_today": notifications_today
        }
    }

@api_router.put("/admin/notification-config")
async def update_notification_config(
    config_update: NotificationScheduleConfig,
    user: User = Depends(get_current_user)
):
    """Update notification scheduling configuration"""
    global NOTIFICATION_CONFIG
    
    update_data = {k: v for k, v in config_update.dict().items() if v is not None}
    NOTIFICATION_CONFIG.update(update_data)
    
    # Save to database for persistence
    await db.app_config.update_one(
        {"config_type": "notifications"},
        {"$set": {"values": NOTIFICATION_CONFIG, "updated_at": datetime.now(timezone.utc)}},
        upsert=True
    )
    
    return {"message": "Notification config updated", "config": NOTIFICATION_CONFIG}

@api_router.post("/admin/send-test-notification")
async def send_test_notification(user: User = Depends(get_current_user)):
    """Send a test notification to the current user"""
    if not user.push_token:
        raise HTTPException(status_code=400, detail="User has no push token registered")
    
    result = await send_expo_push_notification(
        push_tokens=[user.push_token],
        title="🧪 Teste de Notificação",
        body="Esta é uma notificação de teste do LoopNews!",
        data={"type": "test", "timestamp": datetime.now(timezone.utc).isoformat()},
        channel_id="general-news"
    )
    
    return {"message": "Test notification sent", "result": result}

@api_router.post("/admin/send-test-breaking")
async def send_test_breaking_notification(user: User = Depends(get_current_user)):
    """Send a test BREAKING NEWS notification to the current user"""
    if not user.push_token:
        raise HTTPException(status_code=400, detail="User has no push token registered")
    
    result = await send_expo_push_notification(
        push_tokens=[user.push_token],
        title="🚨 URGENTE: Teste de Breaking News",
        body="Esta é uma notificação de teste de notícia urgente! Alta prioridade.",
        data={"type": "urgent_news", "news_id": "test", "urgency_score": 100},
        channel_id="breaking-news"
    )
    
    return {"message": "Breaking news test notification sent", "result": result}

@api_router.post("/admin/send-broadcast")
async def send_broadcast_notification(
    title: str,
    body: str,
    user: User = Depends(get_current_user)
):
    """Send a broadcast notification to all users"""
    # Get all users with push tokens
    users_cursor = db.users.find(
        {"push_token": {"$ne": None}, "notifications_enabled": True},
        {"push_token": 1, "user_id": 1}
    )
    users = await users_cursor.to_list(length=1000)
    
    push_tokens = [u["push_token"] for u in users if u.get("push_token")]
    
    if not push_tokens:
        return {"message": "No users with push tokens found", "sent": 0}
    
    result = await send_expo_push_notification(
        push_tokens=push_tokens,
        title=title,
        body=body,
        data={"type": "broadcast", "timestamp": datetime.now(timezone.utc).isoformat()}
    )
    
    # Create in-app notifications
    for u in users:
        notification = {
            "notification_id": f"notif_{uuid.uuid4().hex[:12]}",
            "user_id": u["user_id"],
            "title": title,
            "body": body,
            "data": {"type": "broadcast"},
            "read": False,
            "created_at": datetime.now(timezone.utc)
        }
        await db.notifications.insert_one(notification)
    
    return {"message": f"Broadcast sent to {len(push_tokens)} users", "result": result}

@api_router.get("/admin/notification-logs")
async def get_notification_logs(
    skip: int = 0,
    limit: int = 50,
    user: User = Depends(get_current_user)
):
    """Get notification sending logs"""
    logs = await db.notification_logs.find(
        {}, {"_id": 0}
    ).sort("timestamp", -1).skip(skip).limit(limit).to_list(length=limit)
    
    return {"logs": logs, "skip": skip, "limit": limit}

@api_router.post("/admin/enhance-images")
async def enhance_all_images(user: User = Depends(get_current_user)):
    """Batch enhance images for existing news that have no image or a generic one"""
    # Find news with missing or empty images
    news_cursor = db.news.find(
        {"$or": [
            {"image_url": ""},
            {"image_url": None},
            {"image_url": {"$exists": False}},
        ]},
        {"_id": 0, "news_id": 1, "title": 1, "source_url": 1, "category": 1, "image_url": 1}
    ).limit(50)
    
    news_to_fix = await news_cursor.to_list(length=50)
    updated = 0
    
    for news in news_to_fix:
        enhanced = await enhance_news_image(news)
        new_image = enhanced.get("image_url", "")
        if new_image and new_image != news.get("image_url", ""):
            await db.news.update_one(
                {"news_id": news["news_id"]},
                {"$set": {"image_url": new_image}}
            )
            updated += 1
    
    return {"message": f"Enhanced {updated} news images out of {len(news_to_fix)} checked"}

# ==================== HEALTH CHECK ====================

@api_router.get("/")
async def root():
    return {"message": "LoopNews API", "version": "1.2.0"}

@api_router.get("/health")
async def health_check():
    # Include scheduler status
    scheduler_jobs = []
    for job in scheduler.get_jobs():
        scheduler_jobs.append({
            "id": job.id,
            "name": job.name,
            "next_run": job.next_run_time.isoformat() if job.next_run_time else None
        })
    
    return {
        "status": "healthy", 
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "scheduler": {
            "running": scheduler.running,
            "jobs": scheduler_jobs
        }
    }

@api_router.get("/scheduler/status")
async def get_scheduler_status():
    """Get detailed scheduler status and job history"""
    jobs = []
    for job in scheduler.get_jobs():
        jobs.append({
            "id": job.id,
            "name": job.name,
            "trigger": str(job.trigger),
            "next_run": job.next_run_time.isoformat() if job.next_run_time else None
        })
    
    # Get last fetch info from database
    last_fetch = await db.scheduler_logs.find_one(
        {"job_id": "news_fetch"},
        sort=[("timestamp", -1)]
    )
    
    return {
        "scheduler_running": scheduler.running,
        "jobs": jobs,
        "last_fetch": {
            "timestamp": last_fetch["timestamp"].isoformat() if last_fetch else None,
            "news_added": last_fetch.get("news_added", 0) if last_fetch else 0,
            "status": last_fetch.get("status", "never_run") if last_fetch else "never_run"
        } if last_fetch else None
    }

@api_router.post("/scheduler/trigger")
async def trigger_news_fetch():
    """Manually trigger news fetch job"""
    result = await scheduled_news_fetch()
    return {"message": "News fetch triggered", "result": result}

# ==================== SCHEDULED TASKS ====================

async def scheduled_news_fetch():
    """Scheduled task to fetch news from all sources"""
    logger.info("🕐 Starting scheduled news fetch...")
    start_time = datetime.now(timezone.utc)
    
    try:
        # Count news before fetch
        old_count = await db.news.count_documents({})
        
        # Fetch from all sources
        await fetch_all_news_sources()
        
        # Fetch for popular categories
        popular_categories = ["tecnologia", "esportes", "economia", "entretenimento", "mundo", "famosos", "futebol"]
        for category in popular_categories:
            try:
                await fetch_all_news_sources(category=category)
            except Exception as e:
                logger.error(f"Error fetching category {category}: {str(e)}")
        
        # Count new news
        new_count = await db.news.count_documents({})
        news_added = new_count - old_count
        
        # Log the fetch
        await db.scheduler_logs.insert_one({
            "job_id": "news_fetch",
            "timestamp": datetime.now(timezone.utc),
            "status": "success",
            "news_added": news_added,
            "duration_seconds": (datetime.now(timezone.utc) - start_time).total_seconds()
        })
        
        logger.info(f"✅ Scheduled news fetch completed. Added {news_added} new articles.")
        
        # Send notifications based on config
        if NOTIFICATION_CONFIG.get("auto_notify_enabled", True):
            min_news = NOTIFICATION_CONFIG.get("min_news_for_notification", 3)
            if news_added >= min_news:
                await notify_users_new_news(news_added)
                
                # Log notification
                await db.notification_logs.insert_one({
                    "type": "auto_new_news",
                    "timestamp": datetime.now(timezone.utc),
                    "news_count": news_added,
                    "status": "sent"
                })
        
        return {"status": "success", "news_added": news_added}
        
    except Exception as e:
        logger.error(f"❌ Scheduled news fetch failed: {str(e)}")
        
        # Log the error
        await db.scheduler_logs.insert_one({
            "job_id": "news_fetch",
            "timestamp": datetime.now(timezone.utc),
            "status": "error",
            "error": str(e),
            "duration_seconds": (datetime.now(timezone.utc) - start_time).total_seconds()
        })
        
        return {"status": "error", "error": str(e)}

async def send_daily_digest():
    """Send daily digest notification to all users"""
    if not NOTIFICATION_CONFIG.get("daily_digest_enabled", True):
        logger.info("📧 Daily digest is disabled, skipping...")
        return
    
    logger.info("📧 Sending daily digest...")
    
    try:
        # Get news from last 24 hours
        yesterday = datetime.now(timezone.utc) - timedelta(hours=24)
        
        # Count today's news by category
        pipeline = [
            {"$match": {"published_at": {"$gte": yesterday}}},
            {"$group": {"_id": "$category", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}}
        ]
        categories = await db.news.aggregate(pipeline).to_list(length=10)
        
        total_news = sum(c["count"] for c in categories)
        
        if total_news == 0:
            logger.info("📧 No news in last 24 hours, skipping digest")
            return
        
        # Get top categories
        top_categories = [c["_id"] for c in categories[:3]]
        categories_text = ", ".join(top_categories) if top_categories else "várias categorias"
        
        # Get users with push tokens
        users_cursor = db.users.find(
            {"push_token": {"$ne": None}, "notifications_enabled": True},
            {"push_token": 1, "user_id": 1, "name": 1, "interests": 1}
        )
        users = await users_cursor.to_list(length=1000)
        
        push_tokens = [u["push_token"] for u in users if u.get("push_token")]
        
        if push_tokens:
            await send_expo_push_notification(
                push_tokens=push_tokens,
                title="📰 Resumo do Dia - LoopNews",
                body=f"{total_news} notícias de {categories_text}. Confira as principais!",
                data={
                    "type": "daily_digest",
                    "total_news": total_news,
                    "categories": top_categories
                }
            )
            
            # Create in-app notifications
            for user in users:
                # Personalize for user interests
                user_interests = user.get("interests", [])
                matching_categories = [c for c in categories if c["_id"] in user_interests]
                
                if matching_categories:
                    body = f"{sum(c['count'] for c in matching_categories)} notícias dos seus interesses disponíveis!"
                else:
                    body = f"{total_news} notícias de {categories_text}. Confira as principais!"
                
                notification = {
                    "notification_id": f"notif_{uuid.uuid4().hex[:12]}",
                    "user_id": user["user_id"],
                    "title": "📰 Resumo do Dia - LoopNews",
                    "body": body,
                    "data": {"type": "daily_digest", "total_news": total_news},
                    "read": False,
                    "created_at": datetime.now(timezone.utc)
                }
                await db.notifications.insert_one(notification)
            
            # Log
            await db.notification_logs.insert_one({
                "type": "daily_digest",
                "timestamp": datetime.now(timezone.utc),
                "users_notified": len(users),
                "total_news": total_news,
                "status": "sent"
            })
            
            logger.info(f"📧 Daily digest sent to {len(users)} users")
        
    except Exception as e:
        logger.error(f"❌ Daily digest failed: {str(e)}")
        await db.notification_logs.insert_one({
            "type": "daily_digest",
            "timestamp": datetime.now(timezone.utc),
            "status": "error",
            "error": str(e)
        })

# ==================== PUSH NOTIFICATIONS ====================

# Palavras-chave para detectar notícias urgentes com scoring
URGENT_KEYWORDS_HIGH = [
    "urgente", "última hora", "breaking", "alerta máximo",
    "terremoto", "tsunami", "acidente fatal", "atentado", "explosão",
    "morre", "falece", "morte de", "morreu", "faleceu",
    "guerra", "invasão", "catástrofe", "tragédia",
]

URGENT_KEYWORDS_MEDIUM = [
    "agora", "ao vivo", "confirmado", "oficial", "emergência",
    "presidente", "ministro", "eleição", "resultado histórico",
    "recorde", "inédito", "decisão final", "alerta",
    "preso", "condenado", "demitido", "renunciou",
]

URGENT_KEYWORDS_LOW = [
    "decisão", "aprovado", "rejeitado", "anúncio",
    "mudança", "novo", "revelado", "descoberto",
]

def get_urgency_score(title: str, summary: str) -> int:
    """Calculate urgency score (0-100) based on keywords"""
    text = f"{title} {summary}".lower()
    score = 0
    
    for keyword in URGENT_KEYWORDS_HIGH:
        if keyword in text:
            score += 40
    
    for keyword in URGENT_KEYWORDS_MEDIUM:
        if keyword in text:
            score += 20
    
    for keyword in URGENT_KEYWORDS_LOW:
        if keyword in text:
            score += 10
    
    return min(score, 100)

def is_urgent_news(title: str, summary: str) -> bool:
    """Check if news is urgent — threshold score of 30+"""
    return get_urgency_score(title, summary) >= 30

async def send_expo_push_notification(push_tokens: list, title: str, body: str, data: dict = None, channel_id: str = "general-news"):
    """Send push notifications via Expo Push API (supports FCM on Android, APNs on iOS)"""
    if not push_tokens:
        return {"success": 0, "failed": 0}
    
    messages = []
    for token in push_tokens:
        if not token or not token.startswith("ExponentPushToken"):
            continue
        
        message = {
            "to": token,
            "sound": "default",
            "title": title,
            "body": body,
            "priority": "high" if channel_id == "breaking-news" else "default",
            "channelId": channel_id,
        }
        
        if data:
            message["data"] = data
        
        # Breaking news: enable critical alert style
        if channel_id == "breaking-news":
            message["badge"] = 1
            message["_contentAvailable"] = True
        
        messages.append(message)
    
    if not messages:
        return {"success": 0, "failed": 0}
    
    try:
        async with httpx.AsyncClient() as http_client:
            success_count = 0
            failed_count = 0
            
            for i in range(0, len(messages), 100):
                batch = messages[i:i+100]
                
                response = await http_client.post(
                    "https://exp.host/--/api/v2/push/send",
                    json=batch,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json"
                    },
                    timeout=30.0
                )
                
                if response.status_code == 200:
                    result = response.json()
                    data_list = result.get("data", [])
                    
                    for item in data_list:
                        if item.get("status") == "ok":
                            success_count += 1
                        else:
                            failed_count += 1
                            logger.warning(f"Push notification failed: {item.get('message', 'Unknown error')}")
                else:
                    failed_count += len(batch)
                    logger.error(f"Expo Push API error: {response.status_code} - {response.text}")
            
            logger.info(f"📲 Push notifications sent: {success_count} success, {failed_count} failed (channel: {channel_id})")
            return {"success": success_count, "failed": failed_count}
            
    except Exception as e:
        logger.error(f"Error sending push notifications: {str(e)}")
        return {"success": 0, "failed": len(messages), "error": str(e)}

async def notify_users_new_news(news_count: int):
    """Notify users about new news via general channel"""
    try:
        users_cursor = db.users.find(
            {"push_token": {"$ne": None}, "notifications_enabled": True},
            {"user_id": 1, "push_token": 1, "name": 1}
        )
        users = await users_cursor.to_list(length=100)
        
        push_tokens = [u["push_token"] for u in users if u.get("push_token")]
        
        if push_tokens:
            await send_expo_push_notification(
                push_tokens=push_tokens,
                title="📰 Novas notícias disponíveis!",
                body=f"{news_count} novas notícias acabaram de chegar. Confira agora!",
                data={"type": "new_news", "count": news_count},
                channel_id="general-news"
            )
        
        # Create in-app notifications
        for user in users:
            notification = {
                "notification_id": f"notif_{uuid.uuid4().hex[:12]}",
                "user_id": user["user_id"],
                "title": "📰 Novas notícias disponíveis!",
                "body": f"{news_count} novas notícias acabaram de chegar. Confira agora!",
                "data": {"type": "new_news", "count": news_count},
                "read": False,
                "created_at": datetime.now(timezone.utc)
            }
            await db.notifications.insert_one(notification)
        
        logger.info(f"📬 Created notifications for {len(users)} users")
        
    except Exception as e:
        logger.error(f"Error sending notifications: {str(e)}")

async def notify_urgent_news(news_item: dict):
    """Send high-priority push notification for urgent/breaking news via breaking-news channel"""
    try:
        users_cursor = db.users.find(
            {"push_token": {"$ne": None}, "notifications_enabled": True},
            {"user_id": 1, "push_token": 1, "interests": 1}
        )
        users = await users_cursor.to_list(length=500)
        
        news_category = news_item.get("category", "").lower()
        urgency_score = get_urgency_score(
            news_item.get("title", ""),
            news_item.get("summary", "")
        )
        
        target_users = []
        for user in users:
            user_interests = [i.lower() for i in user.get("interests", [])]
            # High urgency (60+) sends to ALL users; medium (30+) sends to interested users
            if urgency_score >= 60 or not user_interests or news_category in user_interests or news_category == "geral":
                target_users.append(user)
        
        push_tokens = [u["push_token"] for u in target_users if u.get("push_token")]
        
        if push_tokens:
            result = await send_expo_push_notification(
                push_tokens=push_tokens,
                title="🚨 URGENTE: " + news_item.get("title", "Nova notícia urgente")[:50],
                body=news_item.get("summary", "")[:100] + "...",
                data={
                    "type": "urgent_news",
                    "news_id": news_item.get("news_id"),
                    "category": news_category,
                    "urgency_score": urgency_score,
                },
                channel_id="breaking-news"
            )
            
            # Create in-app notifications
            for user in target_users:
                notification = {
                    "notification_id": f"notif_{uuid.uuid4().hex[:12]}",
                    "user_id": user["user_id"],
                    "title": "🚨 URGENTE: " + news_item.get("title", "")[:50],
                    "body": news_item.get("summary", "")[:150],
                    "data": {
                        "type": "urgent_news",
                        "news_id": news_item.get("news_id"),
                        "category": news_category
                    },
                    "read": False,
                    "created_at": datetime.now(timezone.utc)
                }
                await db.notifications.insert_one(notification)
            
            logger.info(f"🚨 Sent urgent news notification to {len(target_users)} users")
            return result
        
        return {"success": 0, "failed": 0}
        
    except Exception as e:
        logger.error(f"Error sending urgent notification: {str(e)}")
        return {"success": 0, "failed": 0, "error": str(e)}

async def cleanup_old_news():
    """Exclui notícias com mais de 30 dias de publicação"""
    try:
        cutoff_date = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
        
        # Find news IDs to delete for cascade cleanup
        old_news_cursor = db.news.find(
            {"published_at": {"$lt": cutoff_date}},
            {"news_id": 1, "_id": 0}
        )
        old_news_ids = [doc["news_id"] async for doc in old_news_cursor]
        
        if not old_news_ids:
            logger.info("🧹 Cleanup: nenhuma notícia com mais de 30 dias encontrada")
            return
        
        # Delete the news
        result = await db.news.delete_many({"published_at": {"$lt": cutoff_date}})
        deleted_count = result.deleted_count
        
        # Cascade: delete related likes and saves
        await db.news_likes.delete_many({"news_id": {"$in": old_news_ids}})
        await db.saved_news.delete_many({"news_id": {"$in": old_news_ids}})
        
        # Remove from users' viewed lists
        await db.users.update_many(
            {},
            {"$pull": {"viewed_news_ids": {"$in": old_news_ids}}}
        )
        
        logger.info(f"🧹 Cleanup concluído: {deleted_count} notícias com mais de 30 dias excluídas")
    except Exception as e:
        logger.error(f"Erro no cleanup de notícias: {str(e)}")

async def cleanup_duplicate_news():
    """Remove notícias duplicatas periodicamente (por título exato + prefixo de 6 palavras)"""
    try:
        removed = 0
        
        # 1. Exact title duplicates
        pipeline = [
            {"$group": {"_id": "$title", "count": {"$sum": 1}, "ids": {"$push": "$news_id"}}},
            {"$match": {"count": {"$gt": 1}}}
        ]
        async for doc in db.news.aggregate(pipeline):
            ids_to_remove = doc["ids"][1:]
            result = await db.news.delete_many({"news_id": {"$in": ids_to_remove}})
            removed += result.deleted_count
        
        # 2. Near-duplicate by 6-word prefix (catches articles from different sources)
        all_news = await db.news.find(
            {}, {"_id": 1, "title": 1, "news_id": 1, "published_at": 1, "ai_summary": 1, "image_url": 1}
        ).sort("published_at", -1).to_list(15000)
        
        from collections import defaultdict
        prefix_groups = defaultdict(list)
        for n in all_news:
            words = re.sub(r'[^\w\s]', '', n.get("title", "").lower()).split()
            if len(words) >= 6:
                prefix = " ".join(words[:6])
                prefix_groups[prefix].append(n)
        
        for prefix, items in prefix_groups.items():
            if len(items) <= 1:
                continue
            # Keep best: has AI summary + has image + newest
            def quality_score(item):
                score = 0
                if item.get("ai_summary"):
                    score += 10
                if item.get("image_url"):
                    score += 5
                return score
            items.sort(key=lambda x: (quality_score(x), x.get("published_at", "")), reverse=True)
            to_delete = items[1:]
            for item in to_delete:
                await db.news.delete_one({"_id": item["_id"]})
                removed += 1
        
        if removed > 0:
            logger.info(f"🔄 Dedup: {removed} notícias duplicadas removidas")
        else:
            logger.info("🔄 Dedup: nenhuma duplicata encontrada")
    except Exception as e:
        logger.error(f"Erro no dedup: {str(e)}")

async def scheduled_quality_check():
    """Periodic quality check: fix encoding, misclassification, broken images"""
    logger.info("🔍 Starting quality check...")
    import html as html_lib
    
    fixed_count = 0
    
    # 1. Fix encoding issues
    encoding_cursor = db.news.find({"$or": [
        {"title": {"$regex": "Ã¡|Ã©|Ã³|Ãº|â€|&amp;|&lt;|&#"}},
        {"summary": {"$regex": "Ã¡|Ã©|Ã³|Ãº|â€|&amp;|&lt;|&#"}},
    ]})
    async for doc in encoding_cursor:
        updates = {}
        for field in ["title", "summary"]:
            text = doc.get(field, "")
            if not text:
                continue
            fixed = html_lib.unescape(text)
            try:
                if any(c in fixed for c in ["Ã¡", "Ã©", "Ã³", "Ãº", "â€"]):
                    fixed = fixed.encode('latin-1').decode('utf-8')
            except (UnicodeDecodeError, UnicodeEncodeError):
                pass
            fixed = re.sub(r'<[^>]+>', '', fixed)
            fixed = re.sub(r'\s+', ' ', fixed).strip()
            if fixed != text:
                updates[field] = fixed
        if updates:
            await db.news.update_one({"_id": doc["_id"]}, {"$set": updates})
            fixed_count += 1
    
    # 2. Fix misclassified articles using smart_reclassify
    # from news_service import smart_reclassify, SOURCE_FORCED_CATEGORY
    
    for source, correct_cat in SOURCE_FORCED_CATEGORY.items():
        result = await db.news.update_many(
            {"source_name": source, "category": {"$ne": correct_cat}},
            {"$set": {"category": correct_cat}}
        )
        fixed_count += result.modified_count
    
    # 3. Fix health articles in football (but NOT player injuries)
    health_words = ["oncoclínica", "câncer", "diabetes", "dengue", "saúde pública",
                    "sus ", "oms ", "pandemia", "epidemia", "medicamento", "remédio"]
    sport_words = ["jogador", "atleta", "time", "clube", "gol", "partida",
                   "campeonato", "técnico", "escalação", "seleção", "rodada",
                   "liga", "copa", "treino", "lesão", "lesionado", "fratura",
                   "contusão", "desfalque", "messi", "neymar"]
    
    futebol_news = await db.news.find(
        {"category": "futebol"}, {"_id": 1, "title": 1, "summary": 1}
    ).to_list(3000)
    
    for n in futebol_news:
        text = f"{n.get('title', '')} {n.get('summary', '')}".lower()
        if any(w in text for w in health_words) and not any(w in text for w in sport_words):
            await db.news.update_one({"_id": n["_id"]}, {"$set": {"category": "saude"}})
            fixed_count += 1
    
    # 4. Fix missing images with category fallbacks
    FALLBACK_IMAGES = {
        "futebol": "https://images.unsplash.com/photo-1574629810360-7efbbe195018?w=800",
        "tecnologia": "https://images.unsplash.com/photo-1518770660439-4636190af475?w=800",
        "mundo": "https://images.unsplash.com/photo-1451187580459-43490279c0fa?w=800",
        "financas": "https://images.unsplash.com/photo-1611974789855-9c2a0a7236a3?w=800",
        "investimentos": "https://images.unsplash.com/photo-1611974789855-9c2a0a7236a3?w=800",
        "saude": "https://images.unsplash.com/photo-1576091160399-112ba8d25d1d?w=800",
        "famosos": "https://images.unsplash.com/photo-1522069169874-c58ec4b76be5?w=800",
        "games": "https://images.unsplash.com/photo-1542751371-adc38448a05e?w=800",
    }
    DEFAULT_IMG = "https://images.unsplash.com/photo-1504711434969-e33886168d6c?w=800"
    
    no_img_cursor = db.news.find(
        {"$or": [{"image_url": ""}, {"image_url": None}, {"image_url": {"$exists": False}}]},
        {"_id": 1, "category": 1}
    )
    async for doc in no_img_cursor:
        cat = doc.get("category", "geral")
        await db.news.update_one(
            {"_id": doc["_id"]},
            {"$set": {"image_url": FALLBACK_IMAGES.get(cat, DEFAULT_IMG)}}
        )
        fixed_count += 1
    
    # 5. Clean HTML from summaries
    html_cursor = db.news.find({"summary": {"$regex": "<[^>]+>"}})
    async for doc in html_cursor:
        summary = doc.get("summary", "")
        cleaned = re.sub(r'<[^>]+>', '', summary)
        cleaned = html_lib.unescape(cleaned)
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        if cleaned != summary:
            await db.news.update_one({"_id": doc["_id"]}, {"$set": {"summary": cleaned}})
            fixed_count += 1
    
    logger.info(f"✅ Quality check completed. Fixed {fixed_count} articles.")

def setup_scheduler():
    """Configure and start the scheduler with all jobs"""
    
    # Job 1: Fetch news every 30 minutes
    scheduler.add_job(
        scheduled_news_fetch,
        IntervalTrigger(minutes=30),
        id="news_fetch_30min",
        name="Fetch News Every 30 Minutes",
        replace_existing=True
    )
    
    # Job 2: Fetch news at specific hours (6am, 12pm, 6pm, 10pm Brazil time)
    # Brazil is UTC-3, so we adjust
    for hour in [9, 15, 21, 1]:  # UTC hours (6, 12, 18, 22 in Brazil)
        scheduler.add_job(
            scheduled_news_fetch,
            CronTrigger(hour=hour, minute=0),
            id=f"news_fetch_cron_{hour}",
            name=f"Fetch News at {hour}:00 UTC",
            replace_existing=True
        )
    
    # Job 3: Cleanup old news daily at 3am UTC (midnight Brazil)
    scheduler.add_job(
        cleanup_old_news,
        CronTrigger(hour=3, minute=0),
        id="news_cleanup_daily",
        name="Daily News Cleanup",
        replace_existing=True
    )
    
    # Job 4: Daily digest at 11am UTC (8am Brazil time)
    scheduler.add_job(
        send_daily_digest,
        CronTrigger(hour=11, minute=0),
        id="daily_digest",
        name="Daily News Digest (8am Brazil)",
        replace_existing=True
    )
    
    # Job 5: Evening digest at 23pm UTC (8pm Brazil time)
    scheduler.add_job(
        send_daily_digest,
        CronTrigger(hour=23, minute=0),
        id="evening_digest",
        name="Evening News Digest (8pm Brazil)",
        replace_existing=True
    )
    
    # Job 6: Cleanup duplicates every 6 hours
    scheduler.add_job(
        cleanup_duplicate_news,
        IntervalTrigger(hours=6),
        id="dedup_news_6h",
        name="Dedup News Every 6 Hours",
        replace_existing=True
    )
    
    # Job 7: Quality check every 12 hours (fix encoding, misclassification, missing images)
    scheduler.add_job(
        scheduled_quality_check,
        IntervalTrigger(hours=12),
        id="quality_check_12h",
        name="Quality Check Every 12 Hours",
        replace_existing=True
    )
    
    logger.info("📅 Scheduler configured with jobs:")
    for job in scheduler.get_jobs():
        logger.info(f"  - {job.name} (ID: {job.id})")

# ==================== APP LIFESPAN ====================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifespan - startup and shutdown"""
    # Startup
    logger.info("🚀 Starting LoopNews API...")
    
    # Setup and start scheduler
    setup_scheduler()
    scheduler.start()
    logger.info("✅ Scheduler started")
    
    # Run initial news fetch
    logger.info("📰 Running initial news fetch...")
    asyncio.create_task(scheduled_news_fetch())
    
    yield
    
    # Shutdown
    logger.info("🛑 Shutting down LoopNews API...")
    scheduler.shutdown()
    client.close()
    logger.info("✅ Cleanup complete")

# Create the main app with lifespan
app = FastAPI(lifespan=lifespan)

# Create a router with the /api prefix
api_router_instance = APIRouter(prefix="/api")

# Include the router in the main app
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
