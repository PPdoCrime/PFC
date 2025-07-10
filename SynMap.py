import re
import difflib
from qgis.PyQt import QtWidgets, QtCore
from qgis.PyQt.QtWidgets import (
    QFileDialog, QWizard, QWizardPage, QVBoxLayout, QLabel, QPushButton, QComboBox, QApplication,
    QLineEdit, QTableWidget, QTableWidgetItem, QMessageBox, QCompleter, QScrollArea, QRadioButton,
    QVBoxLayout, QHBoxLayout, QRadioButton
)

from qgis.core import QgsVectorLayer, QgsProject, QgsDataSourceUri, QgsApplication, QgsAuthMethodConfig, QgsFields, QgsField, QgsFeature, QgsWkbTypes
from qgis.PyQt.QtCore import QCoreApplication, Qt, QVariant
from qgis.PyQt.QtGui import QGuiApplication, QPixmap, QIcon, QFont
import os


import unicodedata

# Importa fuzzywuzzy para mapeamento por similaridade
try:
    from fuzzywuzzy import fuzz, process
except ImportError:
    fuzz = None
    process = None

# Tenta importar o psycopg2 para conexão com PostGIS
try:
    import psycopg2
except ImportError:
    psycopg2 = None

########################################################################
# FUNÇÃO PARA CONEXÃO COM POSTGIS E EXTRAÇÃO DE TABELAS COM GEOMETRIA
########################################################################
def get_geometry_tables_from_postgis(host, port, dbname, user, password):
    """
    Conecta a um banco PostGIS e retorna um dicionário com as tabelas (schema.tabela)
    que possuem coluna de geometria.
    Exemplo de retorno: { "schema.tabela": "geom", ... }
    """
    if not psycopg2:
        raise ImportError("A biblioteca psycopg2 não está disponível.")
    # Converte os parâmetros para str, caso necessário
    host = str(host)
    port = str(port)
    dbname = str(dbname)
    user = str(user)
    password = str(password)
    conn = psycopg2.connect(host=host, port=port, dbname=dbname, user=user, password=password)
    cur = conn.cursor()
    cur.execute("SELECT f_table_schema, f_table_name, f_geometry_column FROM geometry_columns;")
    results = cur.fetchall()
    tables = {}
    for schema, table, geom_column in results:
        full_table = f"{schema}.{table}"
        tables[full_table] = geom_column
    cur.close()
    conn.close()
    return tables

########################################################################
# FUNÇÃO DE EXTRAÇÃO UNIFICADA DAS CLASSES A PARTIR DO BANCO DE DADOS
########################################################################
def extract_classes_from_db(host, port, dbname, username, password):
    """
    Extrai as classes (tabelas com coluna de geometria) a partir da conexão com o banco PostGIS.
    Retorna um dicionário onde cada chave é o nome da tabela (ou nome ajustado sem sufixo)
    e o valor é a lista de campos.
    """
    tables = get_geometry_tables_from_postgis(host, port, dbname, username, password)
    classes = {}
    for full_table, geom_column in tables.items():
        schema, table_name = full_table.split(".")
        # Não filtra mais por schema; qualquer tabela com geometria é considerada
        uri = QgsDataSourceUri()
        # Converte os parâmetros para string para evitar QVariant
        uri.setConnection(str(host), str(port), str(dbname), str(username), str(password))
        uri.setDataSource(str(schema), str(table_name), str(geom_column))
        layer = QgsVectorLayer(uri.uri(), table_name, "postgres")
        if layer.isValid():
            fields = [field.name() for field in layer.fields()]
            # Se a tabela tiver um sufixo (ex.: "tabela_a"), remova-o para nome padrão
            m = re.match(r'^(.*)_[a-zA-Z]$', table_name)
            class_name = m.group(1) if m else table_name
            classes[class_name] = fields
    return classes

########################################################################
# FUNÇÃO DE NORMALIZAÇÃO
########################################################################
def normalize_str(s):
    nfkd = unicodedata.normalize('NFKD', s)
    no_accent = "".join([c for c in nfkd if not unicodedata.combining(c)])
    return no_accent.lower().replace('_', '').strip()

########################################################################
# FUNÇÃO DE MAPEAMENTO AUTOMÁTICO COM SINÔNIMOS
########################################################################
def auto_map_attributes_with_synonyms(model_attributes, layer_fields, synonyms_dict, threshold=80):
    """
    Mapeia atributos do modelo para os campos da camada de entrada usando correspondência fuzzy.
    Utiliza os sinônimos fornecidos no dicionário synonyms_dict.
    """
    if fuzz is None or process is None:
        raise ImportError("fuzzywuzzy não está disponível para mapeamento automático")
    normalized_layer_fields = [(field, normalize_str(field)) for field in layer_fields]
    normalized_fields = [norm for _, norm in normalized_layer_fields]
    mapping = {}
    used_fields = set()
    for attr in model_attributes:
        norm_attr = normalize_str(attr)
        candidates = [norm_attr]
        if attr.lower() in synonyms_dict:
            for syn in synonyms_dict[attr.lower()]:
                candidates.append(normalize_str(syn))
        candidate_matches = {}
        for candidate in candidates:
            results = process.extract(candidate, normalized_fields, scorer=fuzz.partial_ratio, limit=len(normalized_fields))
            for match, score in results:
                if score >= threshold:
                    if match not in candidate_matches or score > candidate_matches[match]:
                        candidate_matches[match] = score
        sorted_matches = sorted(candidate_matches.items(), key=lambda x: x[1], reverse=True)
        best_match = None
        for match, score in sorted_matches:
            if match not in used_fields:
                best_match = match
                break
        if best_match is not None:
            for original, norm in normalized_layer_fields:
                if norm == best_match:
                    mapping[attr] = original
                    used_fields.add(best_match)
                    break
        else:
            mapping[attr] = None
    return mapping

########################################################################
# PARSER DO SQL - EXTRAÇÃO DAS CLASSES (TABELAS) A PARTIR DO ARQUIVO SQL
########################################################################
def parse_sql(sql_content):
    """
    Extrai as classes (tabelas) a partir de um arquivo SQL.
    Retorna um dicionário: {nome_da_classe: [lista de atributos]}
    """
    table_pattern = re.compile(
        r'CREATE\s+TABLE\s+((?:\w+\.)?(\w+))\s*\((.*?)\)\s*(?:;|#)', 
        re.DOTALL | re.IGNORECASE
    )
    skip_keywords = ('CONSTRAINT', 'PRIMARY', 'FOREIGN', 'UNIQUE', 'CHECK', 'WITH', 'ALTER')
    classes_dict = {}
    for match in table_pattern.finditer(sql_content):
        full_table_name = match.group(1)
        table_name = match.group(2)
        columns_section = match.group(3)
        # Agora, não filtramos por schema; qualquer tabela é considerada
        m = re.match(r'^(.*)_[a-zA-Z]$', table_name)
        class_name = m.group(1) if m else table_name
        lines = columns_section.splitlines()
        columns = []
        for line in lines:
            line = line.strip().rstrip(',')
            if not line or line.upper().startswith(skip_keywords):
                continue
            col_match = re.match(r'^["\']?(\w+)', line)
            if col_match:
                columns.append(col_match.group(1))
        classes_dict[class_name] = columns
    return classes_dict

########################################################################
# Dicionário de sinônimos para mapeamento (ajuste conforme necessário)
########################################################################
SYNONYMS_DICT = {
    "nome": ["name"],
    "name": ["nome"],
    "descricao": ["description", "descrição"],
    "description": ["descricao", "descrição"],
    "id": ["identificador", "id"],
    "codigo": ["code", "codigo"],
}

########################################################################
# PÁGINA 0: SELEÇÃO DA FONTE DE ENTRADA
########################################################################
class InputSourcePage(QWizardPage):
    def __init__(self, parent=None):
        super(InputSourcePage, self).__init__(parent)

        # 1. Configura Título e Subtítulo do Wizard
        self.setTitle("Fonte do Modelo de Dados")
        self.setSubTitle(
            "Escolha a fonte para carregar o modelo de dados:\n"
            " - Arquivo SQL\n"
            " - Conexão já estabelecida no QGIS\n"
            " - Conexão manual ao banco de dados"
        )

        # 2. Layout principal horizontal
        self.main_layout = QHBoxLayout()
        self.setLayout(self.main_layout)

        # 3. Coluna Esquerda (opções do Wizard)
        self.left_layout = QVBoxLayout()
        self.main_layout.addLayout(self.left_layout)

        # Cria os RadioButtons
        self.radioSQL = QRadioButton("Carregar a partir de arquivo SQL")
        self.radioDB = QRadioButton("Utilizar conexão já estabelecida no QGIS")
        self.radioManual = QRadioButton("Conectar manualmente ao banco de dados")
        self.radioSQL.setChecked(True)

        # Adiciona ao layout da esquerda
        self.left_layout.addWidget(self.radioSQL)
        self.left_layout.addWidget(self.radioDB)
        self.left_layout.addWidget(self.radioManual)
        # empurra tudo para o topo, deixando espaço embaixo
        self.left_layout.addStretch()

        # 4. Coluna Direita (logo)
        self.logo_label = QLabel()
        self.logo_label.setAlignment(QtCore.Qt.AlignCenter)

        # Carrega o arquivo de imagem com boa resolução
        logo_path = os.path.join(os.path.dirname(__file__), "resources", "logo_synmap.png")
        pixmap_original = QPixmap(logo_path)

        # 4.1 Escala o pixmap para uma largura maior (ex.: 300 px) mantendo a proporção
        # Se o arquivo for pequeno, pode perder qualidade. Ideal: PNG grande ou SVG.
        pixmap_escalado = pixmap_original.scaledToWidth(
            300,  # largura máxima
            QtCore.Qt.SmoothTransformation
        )

        self.logo_label.setPixmap(pixmap_escalado)
        # Opcionalmente, fixa a largura do label para acomodar a imagem
        self.logo_label.setFixedWidth(320)
        # Não usar scaledContents True, pois pode esticar demais a imagem no layout
        self.logo_label.setScaledContents(False)

        # Adiciona a logo na coluna da direita
        self.main_layout.addWidget(self.logo_label)


    def nextId(self):
        if self.radioSQL.isChecked():
            return 1
        elif self.radioDB.isChecked():
            return 2
        elif self.radioManual.isChecked():
            return 3
        else:
            return -1

    def validatePage(self):
        if self.radioSQL.isChecked():
            self.wizard().inputSource = "SQL"
        elif self.radioDB.isChecked():
            self.wizard().inputSource = "DB"
        else:
            self.wizard().inputSource = "MANUAL"
        return True


########################################################################
# PÁGINA 1: Carregar Modelo a partir de arquivo SQL
########################################################################
class SQLFilePage(QWizardPage):
    def __init__(self, parent=None):
        super(SQLFilePage, self).__init__(parent)
        self.setTitle("Carregar SQL do Modelo")
        self.setSubTitle("Selecione o arquivo SQL que contém o modelo de dados.")
        self.layout = QVBoxLayout(self)
        self.filePathEdit = QLineEdit()
        self.browseButton = QPushButton("Procurar...")
        self.browseButton.clicked.connect(self.browseFile)
        self.layout.addWidget(QLabel("Arquivo SQL:"))
        self.layout.addWidget(self.filePathEdit)
        self.layout.addWidget(self.browseButton)
        self.adjustSize()

    def browseFile(self):
        filePath, _ = QFileDialog.getOpenFileName(
            self, "Selecionar arquivo SQL", "", "SQL Files (*.sql);;All Files (*)"
        )
        if filePath:
            self.filePathEdit.setText(filePath)

    def validatePage(self):
        filePath = self.filePathEdit.text()
        if not filePath:
            QMessageBox.warning(self, "Aviso", "Por favor, selecione um arquivo SQL.")
            return False
        try:
            with open(filePath, 'r', encoding='utf-8') as file:
                sqlContent = file.read()
            classes = parse_sql(sqlContent)
            if not classes:
                QMessageBox.critical(self, "Erro", "Nenhuma tabela/classe identificada no SQL.")
                return False
            self.wizard().classesInfo = classes
            return True
        except Exception as e:
            print("Erro ao ler SQL:", repr(e))
            QMessageBox.critical(self, "Erro", f"Erro ao ler o arquivo: {repr(e)}")
            return False

    def nextId(self):
        return 4  # Próxima página: Seleção de Camada

########################################################################
# PÁGINA 2: Utilizar Conexão já Estabelecida no QGIS
########################################################################
class DBInputPage(QWizardPage):
    def __init__(self, parent=None):
        super(DBInputPage, self).__init__(parent)
        self.setTitle("Utilizar Conexão do QGIS")
        self.setSubTitle("Selecione uma conexão PostgreSQL definida no QGIS para carregar as classes.")
        self.layout = QVBoxLayout(self)
        
        self.connLabel = QLabel("Selecione uma conexão PostgreSQL:")
        self.connCombo = QComboBox()
        
        self.passwordLabel = QLabel("Senha (necessária se não estiver salva na configuração):")
        self.passwordEdit = QLineEdit()
        self.passwordEdit.setEchoMode(QLineEdit.Password)
        
        self.loadButton = QPushButton("Carregar Classes da Conexão Selecionada")
        self.loadButton.clicked.connect(self.loadClasses)
        
        self.layout.addWidget(self.connLabel)
        self.layout.addWidget(self.connCombo)
        self.layout.addWidget(self.passwordLabel)
        self.layout.addWidget(self.passwordEdit)
        self.layout.addWidget(self.loadButton)
        
        # Esconder o campo de senha inicialmente
        self.passwordLabel.hide()
        self.passwordEdit.hide()
        
        self.adjustSize()
        # Limpar classesInfo ao iniciar a página
        if hasattr(self.wizard(), 'classesInfo'):
             self.wizard().classesInfo = {}

    def initializePage(self):
        """ Popula o ComboBox com as conexões PostgreSQL existentes no QGIS. """
        self.connCombo.clear()
        self.available_connections = [] # Armazena apenas os nomes das conexões
        
        settings = QtCore.QSettings()
        settings.beginGroup("PostgreSQL/Connections")
        
        connection_names = settings.childGroups()
        if not connection_names:
            self.connCombo.addItem("Nenhuma conexão PostgreSQL encontrada")
            self.connCombo.setEnabled(False)
            self.loadButton.setEnabled(False)
        else:
            self.connCombo.addItem("") # Adiciona item vazio para seleção inicial
            for conn_name in sorted(connection_names):
                self.connCombo.addItem(conn_name)
                self.available_connections.append(conn_name)
            self.connCombo.setEnabled(True)
            self.loadButton.setEnabled(True)
            
        settings.endGroup()
        
        # Reseta o estado da senha e classes
        self.passwordEdit.clear()
        self.passwordLabel.hide()
        self.passwordEdit.hide()
        if hasattr(self.wizard(), 'classesInfo'):
             self.wizard().classesInfo = {} # Limpa as classes ao (re)inicializar

        self.adjustSize()

    # REMOVIDO: get_existing_postgis_connections (não é mais necessário como antes)

    def loadClasses(self):
        """ Tenta carregar as classes da conexão selecionada, buscando os parâmetros
            corretamente (incluindo authcfg). """
        
        current_conn_name = self.connCombo.currentText()
        if not current_conn_name:
            QMessageBox.warning(self, "Aviso", "Selecione uma conexão PostgreSQL válida.")
            return

        # Limpar classesInfo antes de tentar carregar novamente
        self.wizard().classesInfo = {} 
        
        # --- Início da Lógica de Extração de Parâmetros ---
        host = None
        port = None
        dbname = None
        username = None
        password = None
        authcfg_id = None
        
        settings = QtCore.QSettings()
        settings.beginGroup("PostgreSQL/Connections")
        
        if current_conn_name in settings.childGroups():
            settings.beginGroup(current_conn_name)
            
            host = settings.value("host", "")
            port = settings.value("port", "")
            # QGIS usa 'database' nas settings
            dbname = settings.value("database", "") 
            authcfg_id = settings.value("authcfg", "") 
            
            # Tenta pegar usuário/senha diretamente caso não haja authcfg
            direct_username = settings.value("username", "")
            direct_password = settings.value("password", "") # Geralmente vazio se authcfg é usado
            
            settings.endGroup() # Fecha o grupo da conexão específica
        else:
             QMessageBox.critical(self, "Erro", f"Erro interno: Conexão '{current_conn_name}' não encontrada nas configurações.")
             settings.endGroup() # Fecha PostgreSQL/Connections
             return
             
        settings.endGroup() # Fecha PostgreSQL/Connections
        
        # --- Lógica de Autenticação ---
        password_found = False
        if authcfg_id:
            print(f"Tentando carregar autenticação via AuthConfigId: {authcfg_id}")
            auth_manager = QgsApplication.authManager()
            # Verifica se o Auth Manager está inicializado (deve estar em um plugin)
            if not auth_manager:
                 QMessageBox.critical(self, "Erro Crítico", "QgsApplication.authManager() não está disponível. O ambiente QGIS não está corretamente inicializado?")
                 return

            mconfig = QgsAuthMethodConfig()
            success = auth_manager.loadAuthenticationConfig(authcfg_id, mconfig, full=True)

            if success:
                print("Configuração de autenticação carregada com sucesso via Auth Manager.")
                try:
                    # As chaves 'username' e 'password' são comuns para basic auth
                    username = mconfig.config("username")
                    password = mconfig.config("password")
                    if username is not None and password is not None:
                         password_found = True
                         print(f"Username (AuthCfg): {username}")
                         print(f"Password (AuthCfg): {'*' * len(password) if password else '(vazio)'}") # Mascarada
                    else:
                         print("Username ou Password não encontrados na AuthConfig carregada.")
                except Exception as e:
                    print(f"Erro ao acessar username/password da config '{authcfg_id}': {e}")
                    QMessageBox.warning(self, "Aviso", f"Não foi possível obter username/password da configuração de autenticação '{authcfg_id}'. Verifique o Auth Manager.")
            else:
                print(f"Falha ao carregar a configuração de autenticação para ID: {authcfg_id}")
                QMessageBox.warning(self, "Aviso", f"Não foi possível carregar a configuração de autenticação com ID '{authcfg_id}'. Verifique se ela existe e está correta no Gerenciador de Autenticação do QGIS.")
                # Mesmo que authcfg falhe, podemos tentar a senha direta ou pedir
        
        # Se não usou authcfg ou falhou, tenta usar usuário/senha diretos (se existirem)
        if not password_found:
            print("Tentando usar username/password diretos das configurações (se houver).")
            if direct_username:
                username = direct_username
            if direct_password:
                password = direct_password
                password_found = True # Mesmo que vazia, consideramos encontrada nas settings
                print(f"Username (Direto): {username}")
                print(f"Password (Direto): {'*' * len(password) if password else '(vazio)'}")
        
        # --- Solicitar Senha se Necessário ---
        if not password_found or password is None: # Verifica se a senha ainda é None ou não foi encontrada
            print("Senha não encontrada automaticamente. Solicitando ao usuário.")
            self.passwordLabel.show()
            self.passwordEdit.show()
            self.passwordEdit.setFocus()
            
            provided_pass = self.passwordEdit.text().strip()
            if not provided_pass:
                QMessageBox.information(self, "Senha Necessária", f"A senha para a conexão '{current_conn_name}' não foi encontrada automaticamente. Por favor, insira a senha e clique em 'Carregar Classes' novamente.")
                return # Sai da função para o usuário digitar a senha
            else:
                password = provided_pass
                # Esconde novamente após obter a senha
                self.passwordLabel.hide()
                self.passwordEdit.hide()
        else:
             # Esconde os campos de senha se ela foi encontrada automaticamente
            self.passwordLabel.hide()
            self.passwordEdit.hide()

        # --- Validar Parâmetros Finais ---
        if not all([host, port, dbname, username is not None, password is not None]):
             missing = []
             if not host: missing.append("Host")
             if not port: missing.append("Porta")
             if not dbname: missing.append("Nome do Banco")
             if username is None: missing.append("Usuário")
             if password is None: missing.append("Senha") # Should not happen if logic above is correct
             QMessageBox.critical(self, "Erro", f"Não foi possível obter todos os parâmetros necessários para a conexão '{current_conn_name}'. Faltando: {', '.join(missing)}")
             return
             
        # --- Tentativa de Conexão e Extração ---
        try:
            QApplication.setOverrideCursor(QtCore.Qt.WaitCursor) # Feedback visual
            print(f"Tentando conectar a: Host={host}, Port={port}, DB={dbname}, User={username}, Password={'***' if password else 'None'}")
            
            # Chama a função de extração com os parâmetros corretos
            classes = extract_classes_from_db(host, port, dbname, username, password) 
            
            QApplication.restoreOverrideCursor() # Restaura cursor

            if not classes:
                QMessageBox.information(self, "Informação", f"Nenhuma classe (tabela com geometria) encontrada na conexão '{current_conn_name}' usando os parâmetros fornecidos.")
                self.wizard().classesInfo = {} # Garante que esteja vazio
            else:
                self.wizard().classesInfo = classes
                QMessageBox.information(self, "Sucesso", f"{len(classes)} classes carregadas com sucesso da conexão '{current_conn_name}'!")
                # Forçar a validação da página para habilitar o botão 'Próximo'
                self.completeChanged.emit() 
                
        except ImportError as e:
             QApplication.restoreOverrideCursor()
             QMessageBox.critical(self, "Erro de Dependência", f"Erro ao carregar classes: {e}. Certifique-se que a biblioteca 'psycopg2' está instalada no ambiente Python do QGIS.")
        except Exception as e:
            QApplication.restoreOverrideCursor()
            print(f"Erro detalhado ao conectar/extrair classes (DB: {current_conn_name}): {repr(e)}")
            QMessageBox.critical(self, "Erro de Conexão/Extração", f"Erro ao carregar classes da conexão '{current_conn_name}':\n{repr(e)}\n\nVerifique os parâmetros da conexão, a senha (se solicitada) e se o banco de dados está acessível.")
            self.wizard().classesInfo = {} # Limpa em caso de erro

    def validatePage(self):
        """ Valida se as classes foram carregadas com sucesso. """
        # A validação real acontece após clicar em 'Carregar Classes'.
        # Aqui, apenas verificamos se 'classesInfo' foi populado.
        if hasattr(self.wizard(), 'classesInfo') and self.wizard().classesInfo:
            return True
        else:
             # Não mostra mensagem aqui, pois o feedback é dado no loadClasses
             return False 

    def nextId(self):
        return 4  # Próxima página: Seleção de Camada



########################################################################
# PÁGINA 3: Conexão Manual ao Banco de Dados
########################################################################
class ManualDBInputPage(QWizardPage):
    def __init__(self, parent=None):
        super(ManualDBInputPage, self).__init__(parent)
        self.setTitle("Conexão Manual ao Banco de Dados")
        self.setSubTitle("Insira os parâmetros de conexão para carregar as classes.")
        self.layout = QVBoxLayout(self)
        self.hostLabel = QLabel("Host:")
        self.hostEdit = QLineEdit()
        self.portLabel = QLabel("Porta:")
        self.portEdit = QLineEdit()
        self.dbnameLabel = QLabel("Nome do Banco:")
        self.dbnameEdit = QLineEdit()
        self.usernameLabel = QLabel("Usuário:")
        self.usernameEdit = QLineEdit()
        self.passwordLabel = QLabel("Senha:")
        self.passwordEdit = QLineEdit()
        self.passwordEdit.setEchoMode(QLineEdit.Password)
        self.loadButton = QPushButton("Carregar Classes")
        self.loadButton.clicked.connect(self.loadClasses)
        self.layout.addWidget(self.hostLabel)
        self.layout.addWidget(self.hostEdit)
        self.layout.addWidget(self.portLabel)
        self.layout.addWidget(self.portEdit)
        self.layout.addWidget(self.dbnameLabel)
        self.layout.addWidget(self.dbnameEdit)
        self.layout.addWidget(self.usernameLabel)
        self.layout.addWidget(self.usernameEdit)
        self.layout.addWidget(self.passwordLabel)
        self.layout.addWidget(self.passwordEdit)
        self.layout.addWidget(self.loadButton)
        self.adjustSize()

    def loadClasses(self):
        host = self.hostEdit.text().strip()
        port = self.portEdit.text().strip()
        dbname = self.dbnameEdit.text().strip()
        username = self.usernameEdit.text().strip()
        password = self.passwordEdit.text().strip()
        if not all([host, port, dbname, username, password]):
            QMessageBox.warning(self, "Aviso", "Preencha todos os campos de conexão.")
            return
        try:
            classes = extract_classes_from_db(host, port, dbname, username, password)
            if not classes:
                QMessageBox.information(self, "Informação", "Nenhuma classe encontrada na conexão.")
            else:
                self.wizard().classesInfo = classes
                QMessageBox.information(self, "Sucesso", "Classes carregadas com sucesso!")
        except Exception as e:
            print("Erro ao carregar classes (Manual):", repr(e))
            QMessageBox.critical(self, "Erro", f"Erro ao carregar classes: {repr(e)}")

    def validatePage(self):
        if not self.wizard().classesInfo:
            QMessageBox.warning(self, "Aviso", "Carregue as classes antes de prosseguir.")
            return False
        return True

    def nextId(self):
        return 4  # Próxima página: Seleção de Camada

########################################################################
# PÁGINA 4: Selecionar Camada e Classe de Entrada
########################################################################
class LayerSelectionPage(QWizardPage):
    def __init__(self, parent=None):
        super(LayerSelectionPage, self).__init__(parent)
        self.setTitle("Selecionar Camada e Classe de Entrada")
        self.setSubTitle("Carregue a camada de entrada ou selecione uma já existente no projeto, e escolha a classe desejada.")
        self.layout = QVBoxLayout(self)
        self.loadedLayersLabel = QLabel("Camadas carregadas no QGIS:")
        self.loadedLayersCombo = QComboBox()
        self.loadedLayersCombo.addItem("Nenhuma (usar arquivo)", "")
        self.loadedLayersCombo.currentIndexChanged.connect(self.onLoadedLayerChanged)
        self.layout.addWidget(self.loadedLayersLabel)
        self.layout.addWidget(self.loadedLayersCombo)
        self.fileLabel = QLabel("Arquivo da Camada (ex.: shapefile):")
        self.layerPathEdit = QLineEdit()
        self.layerBrowseButton = QPushButton("Procurar Camada...")
        self.layerBrowseButton.clicked.connect(self.browseLayer)
        self.layout.addWidget(self.fileLabel)
        self.layout.addWidget(self.layerPathEdit)
        self.layout.addWidget(self.layerBrowseButton)
        self.classLabel = QLabel("Selecione a Classe:")
        self.comboClass = QComboBox()
        self.comboClass.setEditable(True)
        self.layout.addWidget(self.classLabel)
        self.layout.addWidget(self.comboClass)
        self.layerFields = []
        self.adjustSize()

    def initializePage(self):
        classesInfo = self.wizard().classesInfo
        if not classesInfo:
            QMessageBox.warning(self, "Aviso", "Nenhuma classe encontrada. Verifique a fonte de entrada.")
            return
        classes = sorted(classesInfo.keys())
        self.comboClass.clear()
        for cls in classes:
            self.comboClass.addItem(cls)
        model = QtCore.QStringListModel(classes, self)
        completer = QCompleter(model, self)
        completer.setCompletionMode(QCompleter.PopupCompletion)
        completer.setCaseSensitivity(QtCore.Qt.CaseInsensitive)
        completer.setFilterMode(QtCore.Qt.MatchContains)
        self.comboClass.setCompleter(completer)
        self.loadedLayersCombo.clear()
        self.loadedLayersCombo.addItem("Nenhuma (usar arquivo)", "")
        for layer in QgsProject.instance().mapLayers().values():
            if isinstance(layer, QgsVectorLayer):
                self.loadedLayersCombo.addItem(layer.name(), layer.id())
        self.loadedLayersCombo.setCurrentIndex(0)
        self.adjustSize()

    def onLoadedLayerChanged(self, index):
        layer_id = self.loadedLayersCombo.itemData(index)
        if layer_id:
            self.layerPathEdit.setText("")

    def browseLayer(self):
        layerPath, _ = QFileDialog.getOpenFileName(
            self, "Selecionar camada", "", "Shapefiles (*.shp);;GeoJSON (*.geojson);;All Files (*)"
        )
        if layerPath:
            self.layerPathEdit.setText(layerPath)
            self.loadedLayersCombo.setCurrentIndex(0)

    def validatePage(self):
        selectedClass = self.comboClass.currentText()
        if not selectedClass:
            QMessageBox.warning(self, "Aviso", "Selecione uma classe.")
            return False
        self.wizard().selectedClass = selectedClass
        layer_id = self.loadedLayersCombo.itemData(self.loadedLayersCombo.currentIndex())
        file_path = self.layerPathEdit.text()
        if layer_id:
            layer = QgsProject.instance().mapLayer(layer_id)
            if not layer or not layer.isValid():
                QMessageBox.warning(self, "Aviso", "A camada selecionada não é válida.")
                return False
            self.layerFields = [field.name() for field in layer.fields()]
            self.wizard().layerFields = self.layerFields
            self.wizard().selectedLayerId = layer_id
            self.wizard().inputLayerPath = ""
        else:
            if not file_path:
                QMessageBox.warning(self, "Aviso", "Selecione uma camada carregada ou um arquivo de camada.")
                return False
            layer = QgsVectorLayer(file_path, "camada_entrada", "ogr")
            if not layer.isValid():
                QMessageBox.critical(self, "Erro", "Camada de arquivo inválida!")
                return False
            self.layerFields = [field.name() for field in layer.fields()]
            self.wizard().layerFields = self.layerFields
            self.wizard().selectedLayerId = ""
            self.wizard().inputLayerPath = file_path
        return True

    def nextId(self):
        return 5  # Próxima página: Mapeamento

########################################################################
# PÁGINA 5: Mapeamento de Atributos
########################################################################
class MappingPage(QWizardPage):
    def __init__(self, parent=None):
        super(MappingPage, self).__init__(parent)
        self.setTitle("Mapeamento de Atributos")
        self.setSubTitle("Associe cada atributo do modelo com um atributo da camada de entrada. Use o botão 'Mapeamento Automático' para sugestões.")
        self.layout = QVBoxLayout(self)
        self.scrollArea = QScrollArea()
        self.scrollArea.setWidgetResizable(True)
        self.mappingTable = QTableWidget()
        self.scrollArea.setWidget(self.mappingTable)
        self.layout.addWidget(self.scrollArea)
        self.autoMapButton = QPushButton("Mapeamento Automático")
        self.autoMapButton.clicked.connect(self.perform_auto_mapping)
        self.layout.addWidget(self.autoMapButton)
        self.adjustSize()

    def initializePage(self):
        classesInfo = self.wizard().classesInfo
        selectedClass = self.wizard().selectedClass
        layerFields = self.wizard().layerFields
        if not (classesInfo and selectedClass and layerFields):
            QMessageBox.critical(self, "Erro", "Dados insuficientes para realizar o mapeamento.")
            return
        self.modelAttributes = classesInfo.get(selectedClass, [])
        self.mappingTable.setColumnCount(2)
        self.mappingTable.setRowCount(len(self.modelAttributes))
        self.mappingTable.setHorizontalHeaderLabels(["Atributo do Modelo", "Atributo da Camada"])
        for row, attr in enumerate(self.modelAttributes):
            edgvItem = QTableWidgetItem(attr)
            edgvItem.setFlags(QtCore.Qt.ItemIsEnabled)
            self.mappingTable.setItem(row, 0, edgvItem)
            combo = QComboBox()
            combo.addItem("")
            for field in layerFields:
                combo.addItem(field)
            self.mappingTable.setCellWidget(row, 1, combo)
        self.mappingTable.horizontalHeader().setStretchLastSection(True)
        self.mappingTable.resizeColumnsToContents()
        self.adjustSize()

    def perform_auto_mapping(self):
        """
        Dispara o mapeamento automático usando fuzzywuzzy:
        - Importa fuzz/process dinamicamente (da sua pasta lib/)
        - Chama a função de mapeamento
        - Atualiza a tabela ou exibe warning
        """
        # ─── 1) Verifica se fuzzywuzzy está disponível ───────────────
        if fuzz is None or process is None:
            QMessageBox.warning(
                self,
                "Mapeamento Automático",
                "Biblioteca fuzzywuzzy não encontrada.\n"
                "Verifique a instalação do plugin SynMap."
            )
            return
        # ──────────────────────────────────────────────────────────────

        # ─── 2) Executa o mapeamento ────────────────────────────────
        layer_fields = self.wizard().layerFields
        auto_mapping = auto_map_attributes_with_synonyms(
            self.modelAttributes,
            layer_fields,
            SYNONYMS_DICT,
            threshold=70
        )
        # ──────────────────────────────────────────────────────────────

        # ─── 3) Preenche a tabela com os resultados ────────────────
        for row in range(self.mappingTable.rowCount()):
            modelo_attr = self.mappingTable.item(row, 0).text()
            mapped_field = auto_mapping.get(modelo_attr)
            if mapped_field:
                combo = self.mappingTable.cellWidget(row, 1)
                idx = combo.findText(mapped_field)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
        # ──────────────────────────────────────────────────────────────

        # ─── 4) Feedback ao usuário ─────────────────────────────────
        used = len([v for v in auto_mapping.values() if v])
        total = len(layer_fields)
        QMessageBox.information(
            self,
            "Mapeamento Automático",
            f"Mapeamento concluído: {used} de {total} atributos automaticamente mapeados."
        )
        # ──────────────────────────────────────────────────────────────



    def validatePage(self):
        mapping = {}
        for row in range(self.mappingTable.rowCount()):
            attr = self.mappingTable.item(row, 0).text()
            combo = self.mappingTable.cellWidget(row, 1)
            selectedField = combo.currentText()
            mapping[attr] = selectedField if selectedField else None
        self.wizard().attributeMapping = mapping
        return True

    def nextId(self):
        return -1  # Última página

########################################################################
# WIZARD PRINCIPAL
########################################################################
class SynMap(QWizard):
    def __init__(self, parent=None):
        super(SynMap, self).__init__(parent)
        self.setFont(QFont("Arial", 11))
        self.setWindowTitle("Wizard de Mapeamento de Dados")
        screen = QGuiApplication.primaryScreen()
        screen_geometry = screen.availableGeometry()
        width = int(screen_geometry.width() * 0.6)
        height = int(screen_geometry.height() * 0.6)
        self.resize(width, height)
        self.setMinimumSize(800, 600)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        # Atributos do wizard
        self.inputSource = None  # "SQL", "DB" ou "MANUAL"
        self.classesInfo = {}
        self.selectedClass = ""
        self.layerFields = []
        self.attributeMapping = {}
        self.selectedLayerId = ""
        self.inputLayerPath = ""
        # Adiciona as páginas conforme o fluxo:
        # Página 0: Escolha da fonte
        # Página 1: SQLFilePage
        # Página 2: DBInputPage
        # Página 3: ManualDBInputPage
        # Página 4: Seleção de Camada
        # Página 5: Mapeamento de atributos
        self.addPage(InputSourcePage())
        self.addPage(SQLFilePage())
        self.addPage(DBInputPage())
        self.addPage(ManualDBInputPage())
        self.addPage(LayerSelectionPage())
        self.addPage(MappingPage())

########################################################################
# CLASSE PRINCIPAL DO PLUGIN (Integração com QGIS)
########################################################################
class SynMapPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.plugin_menu = "&SynMap"

        # ─── 1) Garante que fuzzywuzzy esteja disponível ──────────
        try:
            from fuzzywuzzy import fuzz, process
            self.fuzz    = fuzz
            self.process = process
        except ImportError as e:
            # envia aviso na barra de mensagens do QGIS
            self.iface.messageBar().pushWarning(
                "SynMap",
                f"Dependência fuzzywuzzy não encontrada: {e}"
            )
            # evita NameError em chamadas futuras
            self.fuzz    = None
            self.process = None
        # ─────────────────────────────────────────────────────────────


    def initGui(self):
        # Define o caminho do ícone para a barra de ferramentas (arquivo na pasta 'icons')
        icon_path = os.path.join(os.path.dirname(__file__), 'icons', 'icon.png')
        self.action = QtWidgets.QAction(QIcon(icon_path), "Abrir SynMap", self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        self.iface.addPluginToMenu(self.plugin_menu, self.action)
        self.iface.addToolBarIcon(self.action)


    def unload(self):
        self.iface.removePluginMenu(self.plugin_menu, self.action)
        self.iface.removeToolBarIcon(self.action)

    def run(self):
        wizard = SynMap()
        # Define o ícone da janela do wizard usando a logo
        icon_path = os.path.join(os.path.dirname(__file__), 'resources', 'logo_synmap.png')
        wizard.setWindowIcon(QIcon(icon_path))
        result = wizard.exec_()
        if result != QWizard.Accepted:
            return

        mapping = wizard.attributeMapping  # {modelo_attr: camada_field or None}
        layer_id = wizard.selectedLayerId
        input_path = wizard.inputLayerPath

        # 1) Obter referência ao layer de entrada
        if layer_id:
            in_layer = QgsProject.instance().mapLayer(layer_id)
        else:
            in_layer = QgsVectorLayer(input_path, 'entrada', 'ogr')

        # 2) Lista completa de atributos do modelo EDGV
        model_attrs = list(wizard.classesInfo[wizard.selectedClass])

        # 3) Definir campos da nova camada: campo para cada atributo do modelo
        fields = QgsFields()
        for attr in model_attrs:
            # aqui uso String, mas você pode ajustar o tipo conforme o dicionário de EDGV
            fields.append(QgsField(attr, QVariant.String))

        # 4) Criar camada de memória com mesmo tipo de geometria e CRS
        geom_type = QgsWkbTypes.displayString(in_layer.wkbType())
        crs_wkt = in_layer.crs().toWkt()
        mem_layer = QgsVectorLayer(f"{geom_type}?crs={crs_wkt}", "mapped_output", "memory")
        mem_dp = mem_layer.dataProvider()
        mem_dp.addAttributes(fields)
        mem_layer.updateFields()

        # 5) Preencher feições: geometria + todos os atributos (None onde não mapeado)
        feats = []
        for in_feat in in_layer.getFeatures():
            out_feat = QgsFeature()
            out_feat.setGeometry(in_feat.geometry())

            attrs = []
            for modelo_attr in model_attrs:
                camada_field = mapping.get(modelo_attr)
                if camada_field:
                    # valor real do campo mapeado
                    attrs.append(in_feat[camada_field])
                else:
                    # NULL
                    attrs.append(None)
            out_feat.setAttributes(attrs)
            feats.append(out_feat)

        # 6) Adicionar feições à camada de memória e atualizar extensão
        mem_dp.addFeatures(feats)
        mem_layer.updateExtents()

        # 7) Finalmente, adicionar ao projeto
        QgsProject.instance().addMapLayer(mem_layer)

        QMessageBox.information(
            self.iface.mainWindow(),
            "Camada Criada",
            f"Nova camada 'mapped_output' criada com {len(feats)} feições, com todos os atributos do modelo EDGV (None onde não mapeado)."
        )


########################################################################
# Função para testes independentes (opcional)
########################################################################
def run_wizard():
    app = QtWidgets.QApplication([])
    wizard = SynMap()
    wizard.exec_()
    mapping = wizard.attributeMapping
    print("Mapping realizado:", mapping)

if __name__ == '__main__':
    run_wizard()