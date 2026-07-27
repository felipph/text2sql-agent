-- Seed playground: shard2

CREATE TABLE IF NOT EXISTS recebiveis_618 (
  cnpj VARCHAR(14) NOT NULL,
  valor NUMERIC(14,2) NOT NULL,
  data_vencimento DATE NOT NULL,
  status VARCHAR(20) NOT NULL
);

TRUNCATE TABLE recebiveis_618;

INSERT INTO recebiveis_618 (cnpj, valor, data_vencimento, status) VALUES ('61840781618495', 104.22, '2026-02-05', 'vencido');
INSERT INTO recebiveis_618 (cnpj, valor, data_vencimento, status) VALUES ('61840781618495', 334.02, '2027-01-31', 'pago');
INSERT INTO recebiveis_618 (cnpj, valor, data_vencimento, status) VALUES ('61840781618495', 492.76, '2026-04-30', 'pendente');

CREATE TABLE IF NOT EXISTS recebiveis_654 (
  cnpj VARCHAR(14) NOT NULL,
  valor NUMERIC(14,2) NOT NULL,
  data_vencimento DATE NOT NULL,
  status VARCHAR(20) NOT NULL
);

TRUNCATE TABLE recebiveis_654;

INSERT INTO recebiveis_654 (cnpj, valor, data_vencimento, status) VALUES ('65410433218196', 24.60, '2026-04-22', 'pago');
INSERT INTO recebiveis_654 (cnpj, valor, data_vencimento, status) VALUES ('65410433218196', 257.62, '2026-01-14', 'pago');
INSERT INTO recebiveis_654 (cnpj, valor, data_vencimento, status) VALUES ('65410433218196', 107.43, '2026-11-29', 'vencido');

CREATE TABLE IF NOT EXISTS recebiveis_718 (
  cnpj VARCHAR(14) NOT NULL,
  valor NUMERIC(14,2) NOT NULL,
  data_vencimento DATE NOT NULL,
  status VARCHAR(20) NOT NULL
);

TRUNCATE TABLE recebiveis_718;

INSERT INTO recebiveis_718 (cnpj, valor, data_vencimento, status) VALUES ('71886379402654', 115.50, '2027-01-26', 'pago');
INSERT INTO recebiveis_718 (cnpj, valor, data_vencimento, status) VALUES ('71886379402654', 60.08, '2026-07-14', 'pendente');
INSERT INTO recebiveis_718 (cnpj, valor, data_vencimento, status) VALUES ('71886379402654', 185.90, '2026-06-26', 'pago');
