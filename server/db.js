// State is stored as a single document in MongoDB (Atlas free tier works fine
// here) instead of a local flat file, so it survives redeploys/restarts on
// hosts with an ephemeral filesystem (e.g. Render's free web services).
// Every other file only talks to the functions exported here - state() and
// withState() keep the exact same shape/behavior they always had, so no
// route handler needed to change.

const dns = require('dns');
const { MongoClient } = require('mongodb');

// Some Windows machines' configured DNS servers can't resolve the SRV record
// `mongodb+srv://` needs, even though the OS's own resolver (nslookup) can -
// point Node's resolver at public DNS servers instead. Harmless on hosts
// (like Render) where this was never an issue.
dns.setServers(['1.1.1.1', '8.8.8.8']);

const DEFAULT_PAYMENT_METHODS = [
  { id: 'crypto', label: 'Crypto (USDT/BTC)', details: 'Ask admin to fill in the real wallet address.', enabled: false },
  { id: 'wise', label: 'Bank Transfer (Wise)', details: 'Ask admin to fill in the real account details.', enabled: false },
];

const DOC_ID = 'app_state';
const DB_NAME = process.env.MONGODB_DB || 'lumora_scalping';

let state = null;
let collection = null;

async function init() {
  const uri = process.env.MONGODB_URI;
  if (!uri) {
    throw new Error(
      'MONGODB_URI is not set. Create a free MongoDB Atlas cluster, get its ' +
      'connection string, and set MONGODB_URI (see README).'
    );
  }
  const client = new MongoClient(uri);
  await client.connect();
  collection = client.db(DB_NAME).collection('state');

  const doc = await collection.findOne({ _id: DOC_ID });
  if (doc) {
    delete doc._id;
    state = doc;
    if (!state.paymentMethods) state.paymentMethods = DEFAULT_PAYMENT_METHODS;
  } else {
    state = { clients: [], signals: [], reports: [], nextSignalId: 1, paymentMethods: DEFAULT_PAYMENT_METHODS };
    await collection.insertOne({ _id: DOC_ID, ...state });
  }
}

// Serialize writes so concurrent requests can't stomp on each other
let writeQueue = Promise.resolve();
function withState(mutator) {
  writeQueue = writeQueue.then(async () => {
    const result = mutator(state);
    await collection.replaceOne({ _id: DOC_ID }, { _id: DOC_ID, ...state });
    return result;
  });
  return writeQueue;
}

module.exports = { init, state: () => state, withState };
