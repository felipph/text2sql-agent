-- Seed playground: db_main (clientes)
CREATE TABLE IF NOT EXISTS clientes (
  cnpj VARCHAR(14) PRIMARY KEY,
  razao_social VARCHAR(200) NOT NULL
);

TRUNCATE TABLE clientes;

INSERT INTO clientes (cnpj, razao_social) VALUES ('65410433218196', 'Cliente_000');
INSERT INTO clientes (cnpj, razao_social) VALUES ('71886379402654', 'Cliente_001');
INSERT INTO clientes (cnpj, razao_social) VALUES ('61840781618495', 'Cliente_002');
