-- URL a deployer typed at launch. The deploy command downloads this file
-- instead of the model card's artifact. Null for apps that do not ask for one.
ALTER TABLE deployments ADD COLUMN runtime_model_url TEXT;
