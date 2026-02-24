#include "sqlite_logger.h"
#include <iostream>
#include <sqlite3.h>
#include <vector>
#include <zstd.h>

static std::vector<unsigned char> compress_data(const void *data, size_t size,
                                                const std::string &label) {
  size_t const compressed_bound = ZSTD_compressBound(size);
  std::vector<unsigned char> compressed_buffer(compressed_bound);
  size_t const compressed_size =
      ZSTD_compress(compressed_buffer.data(), compressed_bound, data, size,
                    3); // Level 3 is default

  if (ZSTD_isError(compressed_size)) {
    std::cerr << "Compression failed for " << label << ": "
              << ZSTD_getErrorName(compressed_size) << std::endl;
    return {};
  }
  compressed_buffer.resize(compressed_size);
  if (size > 1024 * 1024) { // Only log for blobs > 1MB
    double ratio = (double)size / compressed_size;
    std::cout << "Compressed " << label
              << " (Zstd): " << size / (1024.0 * 1024.0) << " MB -> "
              << compressed_size / (1024.0 * 1024.0) << " MB (Ratio: " << ratio
              << "x)" << std::endl;
  }
  return compressed_buffer;
}

void save_field_to_db(const RunConfig &cfg, const std::string &field_name,
                      const void *got_data, size_t got_size,
                      const void *want_data, size_t want_size) {
  if (cfg.db_file.empty())
    return;

  auto compressed_got =
      compress_data(got_data, got_size, field_name + " (got)");
  auto compressed_want =
      compress_data(want_data, want_size, field_name + " (want)");

  sqlite3 *db;
  if (sqlite3_open(cfg.db_file.c_str(), &db) != SQLITE_OK)
    return;
  sqlite3_busy_timeout(db, 5000); // Wait up to 5 seconds

  const char *create_sql =
      "CREATE TABLE IF NOT EXISTS fields ("
      "lowerprec TEXT, substeps INTEGER, timestamp INTEGER, nproma INTEGER, "
      "istep INTEGER, lvn_only INTEGER, ldeepatmo INTEGER, field_name TEXT, "
      "got_blob BLOB, want_blob BLOB, "
      "PRIMARY KEY (lowerprec, substeps, timestamp, nproma, istep, lvn_only, "
      "ldeepatmo, field_name));";
  sqlite3_exec(db, create_sql, nullptr, nullptr, nullptr);

  const char *insert_sql =
      "INSERT OR REPLACE INTO fields VALUES (?,?,?,?,?,?,?,?,?,?);";
  sqlite3_stmt *stmt;
  if (sqlite3_prepare_v2(db, insert_sql, -1, &stmt, nullptr) == SQLITE_OK) {
    sqlite3_bind_text(stmt, 1, cfg.lowerprec.c_str(), -1, SQLITE_STATIC);
    sqlite3_bind_int(stmt, 2, cfg.substeps);
    sqlite3_bind_int(stmt, 3, cfg.timestamp);
    sqlite3_bind_int(stmt, 4, cfg.nproma);
    sqlite3_bind_int(stmt, 5, cfg.istep);
    sqlite3_bind_int(stmt, 6, cfg.lvn_only);
    sqlite3_bind_int(stmt, 7, cfg.ldeepatmo);
    sqlite3_bind_text(stmt, 8, field_name.c_str(), -1, SQLITE_STATIC);
    sqlite3_bind_blob(stmt, 9, compressed_got.data(),
                      (int)compressed_got.size(), SQLITE_STATIC);
    sqlite3_bind_blob(stmt, 10, compressed_want.data(),
                      (int)compressed_want.size(), SQLITE_STATIC);
    sqlite3_step(stmt);
    sqlite3_finalize(stmt);
  } else {
    std::cerr << "SQL prepare error: " << sqlite3_errmsg(db) << std::endl;
  }
  sqlite3_close(db);
}

void save_timing_to_db(const RunConfig &cfg, int repetition,
                       const std::string &tag, double time_us) {
  if (cfg.db_file.empty())
    return;
  sqlite3 *db;
  if (sqlite3_open(cfg.db_file.c_str(), &db) != SQLITE_OK)
    return;
  sqlite3_busy_timeout(db, 5000); // Wait up to 5 seconds

  const char *create_sql =
      "CREATE TABLE IF NOT EXISTS timings ("
      "lowerprec TEXT, substeps INTEGER, timestamp INTEGER, nproma INTEGER, "
      "istep INTEGER, lvn_only INTEGER, ldeepatmo INTEGER, repetition INTEGER, "
      "tag TEXT, time_us REAL, "
      "PRIMARY KEY (lowerprec, substeps, timestamp, nproma, istep, lvn_only, "
      "ldeepatmo, repetition, tag));";
  sqlite3_exec(db, create_sql, nullptr, nullptr, nullptr);

  const char *insert_sql =
      "INSERT OR REPLACE INTO timings VALUES (?,?,?,?,?,?,?,?,?,?);";
  sqlite3_stmt *stmt;
  if (sqlite3_prepare_v2(db, insert_sql, -1, &stmt, nullptr) == SQLITE_OK) {
    sqlite3_bind_text(stmt, 1, cfg.lowerprec.c_str(), -1, SQLITE_STATIC);
    sqlite3_bind_int(stmt, 2, cfg.substeps);
    sqlite3_bind_int(stmt, 3, cfg.timestamp);
    sqlite3_bind_int(stmt, 4, cfg.nproma);
    sqlite3_bind_int(stmt, 5, cfg.istep);
    sqlite3_bind_int(stmt, 6, cfg.lvn_only);
    sqlite3_bind_int(stmt, 7, cfg.ldeepatmo);
    sqlite3_bind_int(stmt, 8, repetition);
    sqlite3_bind_text(stmt, 9, tag.c_str(), -1, SQLITE_STATIC);
    sqlite3_bind_double(stmt, 10, time_us);
    sqlite3_step(stmt);
    sqlite3_finalize(stmt);
  } else {
    std::cerr << "SQL prepare error: " << sqlite3_errmsg(db) << std::endl;
  }
  sqlite3_close(db);
}
