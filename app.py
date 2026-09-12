import os
from decimal import Decimal
from datetime import date, datetime
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, flash, session, send_file, jsonify
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from io import BytesIO

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-change-this')
db_url = os.getenv('DATABASE_URL', 'sqlite:///condominio.db')
IS_POSTGRES = db_url.startswith(('postgres://', 'postgresql://'))
DB_SCHEMA = 'vila_rian' if IS_POSTGRES else None
if db_url.startswith('postgres://'):
    db_url = db_url.replace('postgres://', 'postgresql+psycopg://', 1)
if db_url.startswith('postgresql://'):
    db_url = db_url.replace('postgresql://', 'postgresql+psycopg://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 15 * 1024 * 1024
db = SQLAlchemy(app)

def ensure_postgres_schema():
    if IS_POSTGRES:
        with db.engine.begin() as conn:
            conn.exec_driver_sql(f'CREATE SCHEMA IF NOT EXISTS {DB_SCHEMA}')

def table_args():
    return {'schema': DB_SCHEMA} if DB_SCHEMA else {}

class Configuracao(db.Model):
    __table_args__ = table_args()
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(120), nullable=False, default='Condomínio Vila Rian')
    caixa_inicial = db.Column(db.Numeric(12, 2), nullable=False, default=0)

class Usuario(db.Model):
    __table_args__ = table_args()
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(160), unique=True, nullable=False)
    senha_hash = db.Column(db.String(255), nullable=False)
    admin = db.Column(db.Boolean, default=False, nullable=False)
    unidade_id = db.Column(db.Integer, db.ForeignKey(f'{DB_SCHEMA}.unidade.id' if DB_SCHEMA else 'unidade.id'), nullable=True)

class Unidade(db.Model):
    __table_args__ = table_args()
    id = db.Column(db.Integer, primary_key=True)
    numero = db.Column(db.String(30), unique=True, nullable=False)
    responsavel = db.Column(db.String(120), nullable=False)
    valor_mensal = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    ativa = db.Column(db.Boolean, default=True, nullable=False)

class Pagamento(db.Model):
    __table_args__ = table_args()
    id = db.Column(db.Integer, primary_key=True)
    unidade_id = db.Column(db.Integer, db.ForeignKey(f'{DB_SCHEMA}.unidade.id' if DB_SCHEMA else 'unidade.id'), nullable=False)
    competencia = db.Column(db.String(7), nullable=False)  # YYYY-MM
    data_pagamento = db.Column(db.Date, nullable=False)
    valor = db.Column(db.Numeric(12, 2), nullable=False)
    observacao = db.Column(db.String(255))
    unidade = db.relationship('Unidade', backref='pagamentos')

class Despesa(db.Model):
    __table_args__ = table_args()
    id = db.Column(db.Integer, primary_key=True)
    data = db.Column(db.Date, nullable=False)
    categoria = db.Column(db.String(100), nullable=False)
    descricao = db.Column(db.String(255), nullable=False)
    fornecedor = db.Column(db.String(160))
    valor = db.Column(db.Numeric(12, 2), nullable=False)
    observacao = db.Column(db.String(255))

class Documento(db.Model):
    __table_args__ = table_args()
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(255), nullable=False)
    nome_arquivo = db.Column(db.String(255), nullable=False)
    mime_type = db.Column(db.String(120), nullable=False)
    tamanho = db.Column(db.Integer, nullable=False, default=0)
    conteudo = db.Column(db.LargeBinary, nullable=False)
    criado_em = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


def initialize_database_on_startup():
    """Ensure the production PostgreSQL schema and tables exist before requests."""
    with app.app_context():
        ensure_postgres_schema()
        db.create_all()


# Gunicorn imports app.py without executing the __main__ block.
# Initialize the schema/tables during import so production starts cleanly.
initialize_database_on_startup()


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get('admin'):
            flash('Acesso restrito ao administrador.', 'error')
            return redirect(url_for('login'))
        return fn(*args, **kwargs)
    return wrapper

@app.context_processor
def globals_for_templates():
    config = Configuracao.query.first()
    receitas = db.session.query(db.func.coalesce(db.func.sum(Pagamento.valor), 0)).scalar() or 0
    despesas = db.session.query(db.func.coalesce(db.func.sum(Despesa.valor), 0)).scalar() or 0
    caixa = Decimal(config.caixa_inicial if config else 0) + Decimal(receitas) - Decimal(despesas)
    return {'config': config, 'caixa_atual': caixa, 'usuario_admin': session.get('admin', False)}

@app.route('/manifest.json')
def manifest():
    return jsonify({
        'name': 'Condomínio Vila Rian',
        'short_name': 'Vila Rian',
        'start_url': '/',
        'display': 'standalone',
        'background_color': '#ffffff',
        'theme_color': '#111111',
        'icons': [
            {'src': url_for('static', filename='icons/icon-192.png'), 'sizes': '192x192', 'type': 'image/png'},
            {'src': url_for('static', filename='icons/icon-512.png'), 'sizes': '512x512', 'type': 'image/png'},
        ],
    })

@app.route('/')
def index():
    unidades = Unidade.query.filter_by(ativa=True).order_by(Unidade.numero).all()
    ano = int(request.args.get('ano', 2026))
    resumos = {}
    for u in unidades:
        pagamentos_ano = Pagamento.query.filter(
            Pagamento.unidade_id == u.id, Pagamento.competencia.like(f'{ano}-%')
        ).all()
        competencias = {p.competencia for p in pagamentos_ano}
        hoje = date.today()
        meses_vencidos = []
        for mes in range(1, 13):
            # Uma mensalidade só fica em atraso depois do dia 10 do próprio mês.
            if ano < hoje.year or (ano == hoje.year and (mes < hoje.month or (mes == hoje.month and hoje.day > 10))):
                meses_vencidos.append(f'{ano}-{mes:02d}')
        atrasados = [mes for mes in meses_vencidos if mes not in competencias]
        resumos[u.id] = {
            'total': sum((Decimal(p.valor) for p in pagamentos_ano), Decimal('0')),
            'meses': len(competencias),
            'atrasados': len(atrasados),
            'em_dia': len(atrasados) == 0
        }
    # Resumo do balancete do ano para exibição direta na tela inicial.
    config = Configuracao.query.first()
    caixa_inicial = Decimal(config.caixa_inicial if config else 0)
    inicio = date(ano, 1, 1)
    fim = date(ano, 12, 31)
    pagamentos_ano = Pagamento.query.filter(
        Pagamento.data_pagamento >= inicio, Pagamento.data_pagamento <= fim
    ).all()
    despesas_ano = Despesa.query.filter(
        Despesa.data >= inicio, Despesa.data <= fim
    ).all()
    total_receitas = sum((Decimal(p.valor) for p in pagamentos_ano), Decimal('0'))
    total_despesas = sum((Decimal(d.valor) for d in despesas_ano), Decimal('0'))
    saldo = caixa_inicial + total_receitas - total_despesas
    return render_template(
        'dashboard.html', unidades=unidades, resumos=resumos, ano=ano,
        caixa_inicial=caixa_inicial, total_receitas=total_receitas,
        total_despesas=total_despesas, saldo=saldo
    )

@app.route('/balancete')
def balancete():
    ano = int(request.args.get('ano', 2026))
    config = Configuracao.query.first()
    caixa_inicial = Decimal(config.caixa_inicial if config else 0)
    inicio = date(ano, 1, 1)
    fim = date(ano, 12, 31)
    pagamentos = Pagamento.query.filter(
        Pagamento.data_pagamento >= inicio, Pagamento.data_pagamento <= fim
    ).all()
    despesas = Despesa.query.filter(
        Despesa.data >= inicio, Despesa.data <= fim
    ).order_by(Despesa.data.asc(), Despesa.id.asc()).all()
    total_receitas = sum((Decimal(p.valor) for p in pagamentos), Decimal('0'))
    total_despesas = sum((Decimal(d.valor) for d in despesas), Decimal('0'))
    saldo = caixa_inicial + total_receitas - total_despesas
    receitas_por_unidade = {}
    for p in pagamentos:
        receitas_por_unidade[p.unidade_id] = receitas_por_unidade.get(p.unidade_id, Decimal('0')) + Decimal(p.valor)
    unidades = Unidade.query.filter_by(ativa=True).order_by(Unidade.numero).all()
    return render_template('balancete.html', ano=ano, caixa_inicial=caixa_inicial,
                           total_receitas=total_receitas, total_despesas=total_despesas,
                           saldo=saldo, despesas=despesas, unidades=unidades,
                           receitas_por_unidade=receitas_por_unidade)

@app.route('/login', methods=['GET', 'POST'])
def login():
    # A área pública não exige login. Somente a entrada do ADM é protegida.
    if request.method == 'POST':
        senha = request.form.get('senha', '')
        usuario = Usuario.query.filter_by(admin=True).first()
        if usuario and check_password_hash(usuario.senha_hash, senha):
            session['usuario_id'] = usuario.id
            session['admin'] = True
            return redirect(url_for('admin'))
        flash('Senha do administrador inválida.', 'error')
    return render_template('login.html')

@app.route('/sair')
def sair():
    session.clear()
    return redirect(url_for('index'))

@app.route('/documentos')
def documentos():
    documentos = Documento.query.order_by(Documento.criado_em.desc(), Documento.id.desc()).all()
    return render_template('documentos.html', documentos=documentos)

@app.route('/documento/<int:documento_id>')
def abrir_documento(documento_id):
    documento = Documento.query.get_or_404(documento_id)
    # Renderiza um visualizador dentro do próprio sistema, em vez de abrir
    # a imagem/PDF em uma nova aba sem navegação de volta.
    if request.args.get('raw') == '1':
        return send_file(BytesIO(documento.conteudo), mimetype=documento.mime_type,
                         download_name=documento.nome_arquivo, as_attachment=False)
    return render_template('visualizar_documento.html', documento=documento)

@app.route('/admin/documento', methods=['POST'])
@admin_required
def cadastrar_documento():
    arquivo = request.files.get('arquivo')
    nome = request.form.get('nome', '').strip()
    if not arquivo or not arquivo.filename:
        flash('Selecione um arquivo.', 'error')
        return redirect(url_for('admin'))
    if not nome:
        nome = secure_filename(arquivo.filename) or 'Documento'
    extensoes_permitidas = {'.pdf', '.jpg', '.jpeg', '.png', '.webp'}
    original = secure_filename(arquivo.filename)
    extensao = '.' + original.rsplit('.', 1)[1].lower() if '.' in original else ''
    if extensao not in extensoes_permitidas:
        flash('Tipo de arquivo não permitido. Use PDF, JPG, JPEG, PNG ou WEBP.', 'error')
        return redirect(url_for('admin'))
    conteudo = arquivo.read()
    limite = 15 * 1024 * 1024
    if len(conteudo) > limite:
        flash('Arquivo muito grande. O limite é de 15 MB.', 'error')
        return redirect(url_for('admin'))
    mime_type = arquivo.mimetype or 'application/octet-stream'
    permitidos_mime = {'application/pdf', 'image/jpeg', 'image/png', 'image/webp'}
    if mime_type not in permitidos_mime:
        flash('Tipo MIME do arquivo não permitido.', 'error')
        return redirect(url_for('admin'))
    db.session.add(Documento(nome=nome, nome_arquivo=original or nome, mime_type=mime_type,
                             tamanho=len(conteudo), conteudo=conteudo))
    db.session.commit()
    flash('Documento enviado com sucesso.', 'success')
    return redirect(url_for('admin'))

@app.route('/admin/documento/<int:documento_id>/excluir', methods=['POST'])
@admin_required
def excluir_documento(documento_id):
    documento = Documento.query.get_or_404(documento_id)
    db.session.delete(documento)
    db.session.commit()
    flash('Documento excluído com sucesso.', 'success')
    return redirect(url_for('admin'))

@app.route('/unidade/<int:unidade_id>')
def unidade(unidade_id):
    u = Unidade.query.get_or_404(unidade_id)
    ano = int(request.args.get('ano', date.today().year))
    pagamentos = Pagamento.query.filter_by(unidade_id=u.id).order_by(Pagamento.competencia.desc(), Pagamento.data_pagamento.desc()).all()
    pagamentos_ano = [p for p in pagamentos if p.competencia.startswith(f'{ano}-')]
    por_mes = {p.competencia: p for p in pagamentos_ano}
    meses = [
        ('01', 'Janeiro'), ('02', 'Fevereiro'), ('03', 'Março'), ('04', 'Abril'),
        ('05', 'Maio'), ('06', 'Junho'), ('07', 'Julho'), ('08', 'Agosto'),
        ('09', 'Setembro'), ('10', 'Outubro'), ('11', 'Novembro'), ('12', 'Dezembro')
    ]
    hoje = date.today()
    meses_status = []
    for numero, nome in meses:
        competencia = f'{ano}-{numero}'
        p = por_mes.get(competencia)
        mes_num = int(numero)
        vencido = (
            ano < hoje.year or
            (ano == hoje.year and (mes_num < hoje.month or (mes_num == hoje.month and hoje.day > 10)))
        )
        status = 'pago' if p is not None else ('atrasado' if vencido else 'futuro')
        meses_status.append({
            'nome': nome, 'pagamento': p, 'pago': p is not None,
            'vencido': vencido, 'status': status
        })
    total_pago = sum((Decimal(p.valor) for p in pagamentos_ano), Decimal('0'))
    return render_template('unidade.html', unidade=u, pagamentos=pagamentos, total_pago=total_pago,
                           total_pago_ano=total_pago, meses_status=meses_status, ano=ano)

@app.route('/admin')
@admin_required
def admin():
    unidades = Unidade.query.order_by(Unidade.numero).all()
    pagamentos = Pagamento.query.order_by(Pagamento.data_pagamento.desc()).limit(20).all()

    # Despesas: sem limite fixo. O filtro por período permite consultar todo o histórico.
    inicio_raw = request.args.get('despesa_inicio', '').strip()
    fim_raw = request.args.get('despesa_fim', '').strip()
    query = Despesa.query
    inicio = fim = None
    try:
        if inicio_raw:
            inicio = datetime.strptime(inicio_raw, '%Y-%m-%d').date()
            query = query.filter(Despesa.data >= inicio)
        if fim_raw:
            fim = datetime.strptime(fim_raw, '%Y-%m-%d').date()
            query = query.filter(Despesa.data <= fim)
    except ValueError:
        flash('Filtro de data inválido.', 'error')
        inicio = fim = None
        query = Despesa.query
    despesas = query.order_by(Despesa.data.desc(), Despesa.id.desc()).all()
    total_despesas = sum((Decimal(d.valor) for d in despesas), Decimal('0'))
    documentos = Documento.query.order_by(Documento.criado_em.desc(), Documento.id.desc()).all()
    return render_template('admin.html', unidades=unidades, pagamentos=pagamentos, despesas=despesas, documentos=documentos,
                           despesa_inicio=inicio_raw, despesa_fim=fim_raw, total_despesas=total_despesas)

@app.route('/admin/configuracao', methods=['POST'])
@admin_required
def salvar_config():
    config = Configuracao.query.first()
    if not config:
        config = Configuracao(nome='Condomínio Vila Rian')
        db.session.add(config)
    config.caixa_inicial = Decimal(request.form.get('caixa_inicial', '0').replace(',', '.'))
    db.session.commit()
    flash('Caixa inicial atualizado.', 'success')
    return redirect(url_for('admin'))

@app.route('/admin/unidade', methods=['POST'])
@admin_required
def cadastrar_unidade():
    numero = request.form['numero'].strip()
    u = Unidade(numero=numero, responsavel=request.form['responsavel'].strip(), valor_mensal=Decimal(request.form['valor_mensal'].replace(',', '.')))
    db.session.add(u)
    db.session.commit()
    flash('Unidade cadastrada.', 'success')
    return redirect(url_for('admin'))

@app.route('/admin/unidade/<int:unidade_id>/editar', methods=['POST'])
@admin_required
def editar_unidade(unidade_id):
    u = Unidade.query.get_or_404(unidade_id)
    numero = request.form['numero'].strip()
    responsavel = request.form['responsavel'].strip()
    valor_raw = request.form['valor_mensal'].strip().replace('.', '').replace(',', '.')
    try:
        valor = Decimal(valor_raw)
    except Exception:
        flash('Valor mensal inválido.', 'error')
        return redirect(url_for('admin'))
    if not numero or not responsavel:
        flash('Unidade e responsável são obrigatórios.', 'error')
        return redirect(url_for('admin'))
    existente = Unidade.query.filter(Unidade.numero == numero, Unidade.id != u.id).first()
    if existente:
        flash('Já existe outra unidade com esse número.', 'error')
        return redirect(url_for('admin'))
    u.numero = numero
    u.responsavel = responsavel
    u.valor_mensal = valor
    db.session.commit()
    flash('Unidade atualizada com sucesso.', 'success')
    return redirect(url_for('admin'))

@app.route('/admin/pagamento', methods=['POST'])
@admin_required
def cadastrar_pagamento():
    p = Pagamento(
        unidade_id=int(request.form['unidade_id']),
        competencia=request.form['competencia'],
        data_pagamento=datetime.strptime(request.form['data_pagamento'], '%Y-%m-%d').date(),
        valor=Decimal(request.form['valor'].replace(',', '.')),
        observacao=request.form.get('observacao')
    )
    db.session.add(p)
    db.session.commit()
    flash('Pagamento lançado.', 'success')
    return redirect(url_for('admin'))

@app.route('/admin/despesa', methods=['POST'])
@admin_required
def cadastrar_despesa():
    d = Despesa(
        data=datetime.strptime(request.form['data'], '%Y-%m-%d').date(),
        categoria='',
        descricao=request.form['descricao'].strip(),
        fornecedor=request.form.get('fornecedor'),
        valor=Decimal(request.form['valor'].replace(',', '.')),
        observacao=request.form.get('observacao')
    )
    db.session.add(d)
    db.session.commit()
    flash('Despesa lançada.', 'success')
    return redirect(url_for('admin'))

@app.route('/admin/despesa/<int:despesa_id>/editar', methods=['POST'])
@admin_required
def editar_despesa(despesa_id):
    d = Despesa.query.get_or_404(despesa_id)
    try:
        d.data = datetime.strptime(request.form['data'], '%Y-%m-%d').date()
        d.descricao = request.form['descricao'].strip()
        d.valor = Decimal(request.form['valor'].replace('.', '').replace(',', '.'))
    except Exception:
        flash('Data, descrição ou valor inválido.', 'error')
        return redirect(url_for('admin'))
    if not d.descricao:
        flash('A descrição é obrigatória.', 'error')
        return redirect(url_for('admin'))
    db.session.commit()
    flash('Despesa alterada com sucesso.', 'success')
    return redirect(url_for('admin'))

@app.route('/admin/despesa/<int:despesa_id>/excluir', methods=['POST'])
@admin_required
def excluir_despesa(despesa_id):
    d = Despesa.query.get_or_404(despesa_id)
    db.session.delete(d)
    db.session.commit()
    flash('Despesa excluída com sucesso.', 'success')
    return redirect(url_for('admin'))

@app.cli.command('init-db')
def init_db():
    ensure_postgres_schema()
    db.create_all()
    if not Configuracao.query.first():
        db.session.add(Configuracao(nome='Condomínio Vila Rian', caixa_inicial=0))
    if not Usuario.query.filter_by(email='admin@vilaryan.local').first():
        db.session.add(Usuario(nome='Administrador', email='admin@vilaryan.local', senha_hash=generate_password_hash('troque-esta-senha'), admin=True))
    db.session.commit()
    print('Banco inicializado. Login: admin@vilaryan.local / troque-esta-senha')

@app.cli.command('importar-pagamentos-2026')
def importar_pagamentos_2026():
    """Importa os pagamentos de 2026 informados na planilha/imagem do condomínio.
    Não duplica pagamento para a mesma unidade e competência e tenta reconhecer
    os números das unidades já cadastradas (ex.: 4/101, 4/102, 7/101, 7/102).
    """
    dados = [
        ('1', 'Paula', ['01','02','03','04','05','06']),
        ('2', 'Maurício', ['01','02','03','04','05']),
        ('3', 'Edson', ['01','02','03','04','05','06','07','08','09','10','11','12']),
        ('4/101', 'Marcos', ['01','02','03','04','05','06','07','08']),
        ('4/102', 'Marcos', ['01','02','03','04','05','06','07','08','09','10']),
        ('5', 'Luciene', ['01','02','03','04','05','06','07','08','09']),
        ('6', 'André', ['01','02','03','04','05','06','07','08','09']),
        ('7/101', 'Marcos', ['01','02','03','04','05','06','07','08','09']),
        ('7/102', 'Marcos', ['01','02','03','04','05','06','07','08','09']),
        ('8', 'Janaina', ['01','02','03','04','05','06','07','08','09']),
    ]
    criados = 0
    total = Decimal('0')
    for numero, responsavel, meses in dados:
        u = Unidade.query.filter_by(numero=numero).first()
        # Compatibilidade com versões anteriores que podem ter usado "Casa 1" etc.
        if not u:
            candidatos = [f'Casa {numero}', numero]
            for candidato in candidatos:
                u = Unidade.query.filter_by(numero=candidato).first()
                if u:
                    break
        if not u:
            u = Unidade(numero=numero, responsavel=responsavel, valor_mensal=Decimal('40.00'), ativa=True)
            db.session.add(u)
            db.session.flush()
        else:
            u.responsavel = responsavel
            u.valor_mensal = Decimal('40.00')
            u.ativa = True
            # Normaliza apenas os formatos antigos simples "Casa N".
            if u.numero == f'Casa {numero}':
                u.numero = numero
        for mes in meses:
            competencia = f'2026-{mes}'
            if Pagamento.query.filter_by(unidade_id=u.id, competencia=competencia).first():
                continue
            db.session.add(Pagamento(
                unidade_id=u.id,
                competencia=competencia,
                data_pagamento=date(2026, int(mes), 5),
                valor=Decimal('40.00'),
                observacao='Importado da planilha do Condomínio Vila Rian'
            ))
            criados += 1
            total += Decimal('40.00')
    db.session.commit()
    print(f'Importação concluída: {criados} pagamentos, total de R$ {total:,.2f}.')

@app.route('/admin/importar-pagamentos-2026', methods=['POST'])
@admin_required
def importar_pagamentos_2026_web():
    # Mantém a importação segura/idempotente também pela tela administrativa.
    dados = [
        ('1', 'Paula', ['01','02','03','04','05','06']),
        ('2', 'Maurício', ['01','02','03','04','05']),
        ('3', 'Edson', ['01','02','03','04','05','06','07','08','09','10','11','12']),
        ('4/101', 'Marcos', ['01','02','03','04','05','06','07','08']),
        ('4/102', 'Marcos', ['01','02','03','04','05','06','07','08','09','10']),
        ('5', 'Luciene', ['01','02','03','04','05','06','07','08','09']),
        ('6', 'André', ['01','02','03','04','05','06','07','08','09']),
        ('7/101', 'Marcos', ['01','02','03','04','05','06','07','08','09']),
        ('7/102', 'Marcos', ['01','02','03','04','05','06','07','08','09']),
        ('8', 'Janaina', ['01','02','03','04','05','06','07','08','09']),
    ]
    criados = 0
    total = Decimal('0')
    for numero, responsavel, meses in dados:
        u = Unidade.query.filter_by(numero=numero).first()
        if not u:
            for candidato in (f'Casa {numero}', numero):
                u = Unidade.query.filter_by(numero=candidato).first()
                if u:
                    break
        if not u:
            flash(f'Unidade {numero} não encontrada. Cadastre-a antes da importação.', 'error')
            return redirect(url_for('admin'))
        if u.numero == f'Casa {numero}':
            u.numero = numero
        for mes in meses:
            competencia = f'2026-{mes}'
            if Pagamento.query.filter_by(unidade_id=u.id, competencia=competencia).first():
                continue
            db.session.add(Pagamento(
                unidade_id=u.id,
                competencia=competencia,
                data_pagamento=date(2026, int(mes), 5),
                valor=Decimal('40.00'),
                observacao='Importado da planilha do Condomínio Vila Rian'
            ))
            criados += 1
            total += Decimal('40.00')
    db.session.commit()
    flash(f'Importação concluída: {criados} pagamentos, total de R$ {total:,.2f}.', 'success')
    return redirect(url_for('admin'))

if __name__ == '__main__':
    with app.app_context():
        ensure_postgres_schema()
        db.create_all()
        config = Configuracao.query.first()
        if config:
            config.nome = 'Condomínio Vila Rian'
            db.session.commit()
    app.run(host='0.0.0.0', port=5000, debug=True)
