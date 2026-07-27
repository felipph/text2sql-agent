-- Seed playground: shard2

CREATE TABLE IF NOT EXISTS recebiveis_556 (
  cnpj VARCHAR(14) NOT NULL,
  valor NUMERIC(14,2) NOT NULL,
  data_vencimento DATE NOT NULL,
  status VARCHAR(20) NOT NULL
);

TRUNCATE TABLE recebiveis_556;

INSERT INTO recebiveis_556 (cnpj, valor, data_vencimento, status) VALUES ('55667788000111', 200.00, '2026-01-20', 'pago');
INSERT INTO recebiveis_556 (cnpj, valor, data_vencimento, status) VALUES ('55667788000111', 80.00, '2026-03-01', 'pendente');

CREATE TABLE IF NOT EXISTS recebiveis_999 (
  cnpj VARCHAR(14) NOT NULL,
  valor NUMERIC(14,2) NOT NULL,
  data_vencimento DATE NOT NULL,
  status VARCHAR(20) NOT NULL
);

TRUNCATE TABLE recebiveis_999;

INSERT INTO recebiveis_999 (cnpj, valor, data_vencimento, status) VALUES ('99988877000155', 40.00, '2025-12-01', 'vencido');
