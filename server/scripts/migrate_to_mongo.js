// One-time import of the existing local data/db.json (real clients, signals,
// reports) into MongoDB. Run this once, after setting MONGODB_URI, before
// switching the live server over to the Mongo-backed db.js.
//
//   MONGODB_URI="<your Atlas connection string>" node scripts/migrate_to_mongo.js
'use strict';

const fs = require('fs');
const path = require('path');
const dns = require('dns');
const { MongoClient } = require('mongodb');

dns.setServers(['1.1.1.1', '8.8.8.8']);

const FILE = path.join(__dirname, '..', 'data', 'db.json');
const DOC_ID = 'app_state';
const DB_NAME = process.env.MONGODB_DB || 'lumora_scalping';

async function main() {
  const uri = process.env.MONGODB_URI;
  if (!uri) {
    console.error('Set MONGODB_URI first.');
    process.exit(1);
  }
  if (!fs.existsSync(FILE)) {
    console.error(`No local file found at ${FILE} - nothing to migrate.`);
    process.exit(1);
  }

  const data = JSON.parse(fs.readFileSync(FILE, 'utf8'));
  console.log(`Loaded local db.json: ${data.clients.length} clients, ${data.signals.length} signals, ${data.reports.length} reports.`);

  const client = new MongoClient(uri);
  await client.connect();
  const collection = client.db(DB_NAME).collection('state');

  const existing = await collection.findOne({ _id: DOC_ID });
  if (existing) {
    console.error(
      'A state document already exists in MongoDB. Refusing to overwrite it ' +
      'automatically - delete it first in Atlas if you really want to replace it, ' +
      'or this was already migrated.'
    );
    process.exit(1);
  }

  await collection.insertOne({ _id: DOC_ID, ...data });
  console.log('Migration complete.');
  await client.close();
}

main().catch(err => {
  console.error(err);
  process.exit(1);
});
