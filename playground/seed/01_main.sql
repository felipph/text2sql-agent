-- Seed playground: db_main (clientes)
CREATE TABLE IF NOT EXISTS clientes (
  cnpj VARCHAR(14) PRIMARY KEY,
  razao_social VARCHAR(200) NOT NULL
);

TRUNCATE TABLE clientes;

INSERT INTO clientes (cnpj, razao_social) VALUES ('12345678000190', 'ACME');
INSERT INTO clientes (cnpj, razao_social) VALUES ('55667788000111', 'Beta');
INSERT INTO clientes (cnpj, razao_social) VALUES ('99988877000155', 'Gama');
