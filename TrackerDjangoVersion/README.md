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

Vars de ambiente: `SECRET_KEY`, `DEBUG=False`, `ALLOWED_HOSTS`, `OPENAI_API_KEY` (opcional para IA), `OPENAI_MODEL` (opcional).

## Importação (CSV/PDF)
- CSV: colunas `data`, `descrição`, `valor`, `tipo (income/expense opcional)`, `categoria opcional`; delimitador `,` ou `;`.
- PDF: extratos com linhas `data` em uma linha e `hora descrição valor` na linha seguinte (PicPay) ou tudo na mesma linha. PDFs de imagem precisam ser convertidos para CSV/Excel.
- Tela de importação mostra preview, permite remover/destacar e aplicar categorias sugeridas (IA/heurística).

## Comandos úteis (Makefile)
- `make run` — servidor.
- `make lint` — ruff/isort/black (modo check).
- `make test` — pytest.
- `make import-demo` — importa dados legados (ajuste caminho com `--db` se necessário).

## Observações
- IA de categorização registra motivo; se `OPENAI_API_KEY` não estiver definida, usa heurísticas.
- Ícone de agente flutuante abre o chat embed em todas as páginas.
- Para produção: habilite `SECURE_*`, `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, sirva estáticos com Whitenoise e configure cache em endpoints pesados.
