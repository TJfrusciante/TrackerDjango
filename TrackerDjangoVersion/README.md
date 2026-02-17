# Tracker Django

## Setup rápido
```bash
python -m venv .venv
source .venv/Scripts/activate  # Windows adapte
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Vars de ambiente: `SECRET_KEY`, `DEBUG=False`, `ALLOWED_HOSTS`, `OPENAI_API_KEY` (opcional para IA), `AI_MODEL` (opcional; fallback legado: `OPENAI_MODEL`).

## Importação (CSV/PDF)
- CSV: colunas `data`, `descrição`, `valor`, `tipo (income/expense opcional)`, `categoria opcional`; delimitador `,` ou `;`.
- PDF: extratos com linhas `data` em uma linha e `hora descrição valor` na linha seguinte (PicPay) ou tudo na mesma linha. PDFs de imagem precisam ser convertidos para CSV/Excel.
- Tela de importação mostra preview, permite remover/destacar e aplicar categorias sugeridas (IA/heurística).

## WhatsApp Finance (Providers)
- Webhook: `POST /whatsapp/webhook/`.
- Provider: `WHATSAPP_PROVIDER=360dialog` ou `twilio` (legado).
- 360dialog: `D360_API_KEY`, `D360_BASE_URL` (default `https://waba-v2.360dialog.io`), `D360_PHONE_NUMBER_ID` (se necessário), `WHATSAPP_WEBHOOK_SECRET`.
- Twilio (legado): `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_NUMBER` (opcional), `WHATSAPP_VALIDATE_TWILIO=false`.
- Perfil: crie um `WhatsAppProfile` no admin e informe o telefone no formato `+55...` e o workspace.
- Comandos: `/resumo`, `/categorias`, `/extrato 01/01/2026 31/01/2026`.
- Audio: `WHATSAPP_TRANSCRIBE_PROVIDER=whisper` (instalar `openai-whisper` + ffmpeg) ou `google` (instalar `google-cloud-speech`).
- OCR: requer `pytesseract` + Tesseract instalado no sistema (Windows: installer oficial).

## Comandos úteis (Makefile)
- `make run` — servidor.
- `make lint` — ruff/isort/black (modo check).
- `make test` — pytest.
- `make import-demo` — importa dados legados (ajuste caminho com `--db` se necessário).

## Observações
- IA de categorização registra motivo; se `OPENAI_API_KEY` não estiver definida, usa heurísticas.
- Ícone de agente flutuante abre o chat embed em todas as páginas.
- Para produção: habilite `SECURE_*`, `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, sirva estáticos com Whitenoise e configure cache em endpoints pesados.
- Dashboard: filtro por responsável (membro) e cores de categorias preservadas quando filtra gráficos por categoria.
- Tarefas: campo responsável (membro do workspace) disponível no formulário e nas ações em massa.
- Transações: ação em massa permite selecionar todos os itens do filtro (não só a página atual).
