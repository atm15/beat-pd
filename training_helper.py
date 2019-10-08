from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import StratifiedKFold, RepeatedStratifiedKFold, GridSearchCV
from sklearn.metrics import confusion_matrix, roc_auc_score, mean_absolute_error, mean_squared_error
from sklearn.preprocessing import label_binarize
import mord
from ordinal_model import OrdinalClassifier
import xgboost as xgb
import scipy.stats
from sklearn.pipeline import Pipeline
from settings import *

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)


def train_user_model(data, label_name, model_type):
    print('Model:', model_type, ', Label:', label_name)
    ground_truths, preds = np.array([]), np.array([])
    data_quantity = pd.DataFrame(columns=['subject_id', 'samples'])
    scores = pd.DataFrame(columns=['subject_id', 'AUC', 'MSE', 'MAE', 'MSE_gain', 'MAE_gain'])
    sorted_subjects = sorted(data.subject_id.unique())
    for subject in sorted_subjects[:3]:
        print_debug('--------------')
        print('Subject: %s' % subject)

        # Get data belonging to a specific subject
        subj_data = data[data.subject_id == subject].copy()
        subj_data.sort_values(by='timestamp', inplace=True)

        # Remove cases where on_off is not labeled
        subj_data = subj_data[subj_data.on_off > -1]

        # Make a table that just has unique measurement_ids and labels for the user
        id_table = subj_data[['ID', label_name]].drop_duplicates()
        data_quantity = data_quantity.append({'subject_id': subject, 'samples': len(id_table)}, ignore_index=True)

        # Remove any classes with not enough samples
        label_counts = id_table[label_name].value_counts()
        for i in range(len(label_counts)):
            if i in label_counts and label_counts[i] <= MIN_OBSERVATIONS_PER_CLASS:
                subj_data = subj_data[subj_data[label_name] != i]
                id_table = id_table[id_table[label_name] != i]
                print_debug('Removing class %d from this user' % i)

        # Skip if not enough data left over
        if len(id_table) <= MIN_OBSERVATIONS_PER_SUBJECT:
            print_debug('Not enough data points for that subject')
            continue

        rskf = RepeatedStratifiedKFold(n_splits=NUM_STRATIFIED_FOLDS, n_repeats=NUM_STRATIFIED_ROUNDS, random_state=RANDOM_SEED)
        for fold_idx, (train_idxs, test_idxs) in enumerate(list(rskf.split(id_table.ID, id_table[label_name]))):
            print_debug('Round: %d, Fold %d' % (int(fold_idx/NUM_STRATIFIED_FOLDS)+1,
                                                (fold_idx % NUM_STRATIFIED_FOLDS)+1))

            # Get measurement_ids for each fold
            id_train_set = id_table.ID.values[train_idxs]
            id_test_set = id_table.ID.values[test_idxs]

            # Separate train and test
            subj_data_train = subj_data[subj_data['ID'].isin(id_train_set)]
            subj_data_test = subj_data[subj_data['ID'].isin(id_test_set)]
            id_test = subj_data_test.ID.values

            # Separate into features and labels
            x_train = subj_data_train.iloc[:, :-7].values
            x_test = subj_data_test.iloc[:, :-7].values
            y_train = subj_data_train[label_name].values
            y_test = subj_data_test[label_name].values
            train_classes, test_classes = np.unique(y_train), np.unique(y_test)

            # Make sure that folds don't cut the data in a weird way
            if len(train_classes) <= 1:
                print_debug('Not enough classes in train')
                continue
            if len(test_classes) <= 1:
                print_debug('Not enough classes in test')
                continue
            if any([c not in train_classes for c in test_classes]):
                print_debug('There is a test class that is not in train')
                continue

            # Pick the correct model
            if model_type == RANDOM_FOREST:
                model = RandomForestClassifier(random_state=RANDOM_SEED)
                param_grid = dict(regression__n_estimators=np.arange(10, 51, 10))
            elif model_type == XGBOOST:
                model = xgb.XGBClassifier(objective="multi:softprob", random_state=RANDOM_SEED)
                model.set_params(**{'num_class': len(train_classes)})
                param_grid = dict(regression__n_estimators=np.arange(80, 121, 20))
            elif model_type == ORDINAL_RANDOM_FOREST:
                model = RandomForestClassifier(random_state=RANDOM_SEED)
                param_grid = dict(regression__n_estimators=np.arange(10, 51, 10))
            elif model_type == ORDINAL:
                model = mord.LogisticSE()
                param_grid = dict(regression__alpha=np.arange(1, 6, 1))
                missing_class = any([k != train_classes[k] for k in range(len(train_classes))])
                if missing_class:
                    # TODO: map classes?
                    print_debug('Missing a class, what should we do?')
                    continue
            else:
                raise Exception('Not a valid model type')

            # Identify ideal parameters using stratified k-fold cross-validation
            cross_validator = StratifiedKFold(n_splits=PARAM_SEARCH_FOLDS, random_state=RANDOM_SEED)
            pipeline = Pipeline([
                ('regression', model)
            ])
            grid_search = GridSearchCV(pipeline, param_grid=param_grid, cv=cross_validator)
            grid_search.fit(x_train, y_train)
            model = pipeline.set_params(**grid_search.best_params_)
            print_debug('Done cross-validating')

            # Fit the model and predict classes
            model.fit(x_train, y_train)
            pred = model.predict(x_test)
            probs = model.predict_proba(x_test)
            lab = model.classes_

            # Concatenate results
            ground_truths = np.concatenate([ground_truths, y_test])
            preds = np.concatenate([preds, pred])

            # Show subject confusion matrix
            # if DEBUG:
            #     plot_confusion(y_test, pred, 'Model: %s, Label: %s\nSubject: %s'
            #                    % (model_type, label_name, subject))

            # Bin probabilities over each diary entry
            probs_bin = []
            y_test_bin = []
            pred_bin = []
            for ID in np.unique(id_test):
                probs_bin.append(np.mean(probs[id_test == ID, :], axis=0).reshape([1, -1]))
                y_test_bin.append(np.mean(y_test[id_test == ID]))
                pred_bin.append(np.mean(pred[id_test == ID]))
            probs_bin = np.vstack(probs_bin)
            y_test_bin = np.vstack(y_test_bin)
            pred_bin = np.vstack(pred_bin)

            # Binarize the results
            y_test_binary = label_binarize(y_test, lab)
            y_test_bin_binary = label_binarize(y_test_bin, lab)

            # Drop probabilities for classes not found in test data
            for i in list(range(np.shape(y_test_binary)[1]))[::-1]:
                if not any(y_test_bin_binary[:, i]):
                    probs = np.delete(probs, i, axis=1)
                    probs_bin = np.delete(probs_bin, i, axis=1)
                    y_test_binary = np.delete(y_test_binary, i, axis=1)
                    y_test_bin_binary = np.delete(y_test_bin_binary, i, axis=1)

            # Calculate MSE/MAE
            mse = mean_squared_error(y_test_bin, pred_bin)
            mae = mean_absolute_error(y_test_bin, pred_bin)

            # Compute null model MSE/MAE
            null_model_mse = mean_squared_error(np.ones(pred_bin.shape)*np.mean(y_train), pred_bin)
            null_model_mae = mean_absolute_error(np.ones(pred_bin.shape)*np.median(y_train), pred_bin)

            # Calculate MSE/MAE gain
            mse_gain = mse-null_model_mse
            mae_gain = mae-null_model_mae

            # Calculate AUCs
            if len(lab) > 2:
                auc = roc_auc_score(y_test_bin_binary, probs_bin, average='weighted')
            else:
                auc = roc_auc_score(y_test_bin_binary, probs_bin[:, 0], average='weighted')
            print_debug('AUC: %0.2f' % auc)

            # Add scores
            scores = scores.append({'subject_id': subject, 'AUC': auc,
                                    'MSE': mse, 'MAE': mae,
                                    'MSE_gain': mse_gain, 'MAE_gain': mae_gain},
                                   ignore_index=True)

    # Compute means and CIs
    auc_mean, auc_stderr = compute_mean_ci(scores.AUC)
    mse_mean, mse_stderr = compute_mean_ci(scores.MSE)
    mae_mean, mae_stderr = compute_mean_ci(scores.MAE)

    # Stack MSE/MAE for second plot
    scores_plot = scores.copy()
    scores_plot = scores_plot.melt(id_vars='subject_id', value_vars=["MSE_gain", "MAE_gain"])

    # Create titles
    title1 = 'Model: %s, Label: %s\n' % (model_type, label_name)
    title1 += 'AUC = %0.2f±%0.2f' % (auc_mean, auc_stderr)

    title2 = 'Model: %s, Label: %s\n' % (model_type, label_name)
    title2 += 'MSE = %0.2f±%0.2f, MAE = %0.2f±%0.2f' % (mse_mean, mse_stderr, mae_mean, mae_stderr)

    # Create x-ticks
    x_ticks = ['%d (%d)' % (subj, quant) for subj, quant in zip(data_quantity.subject_id.values, data_quantity.samples.values)
               if subj in scores.subject_id.values]

    # Plot boxplot of AUCs
    sns.set(style="whitegrid")
    fig = plt.figure()
    ax = fig.add_subplot(121)
    sns.boxplot(x='subject_id', y='AUC', data=scores)
    plt.title(title1)
    plt.axhline(0.5, 0, len(sorted_subjects), color='k', linestyle='--')
    ax.set_xticklabels(x_ticks), plt.setp(ax.xaxis.get_majorticklabels(), rotation=90)
    plt.xlabel('Subject ID (#samples)')
    plt.ylabel('AUC'), plt.ylim(0, 1)

    ax = fig.add_subplot(122)
    sns.boxplot(x='subject_id', y='value', data=scores_plot, hue='variable')
    plt.title(title2)
    ax.set_xticklabels(x_ticks), plt.setp(ax.xaxis.get_majorticklabels(), rotation=90)
    plt.xlabel('Subject ID (#samples)'), plt.ylabel('Gain')

    plt.savefig(os.path.join('figs', '%s_%s.png' % (model_type, label_name)), bbox_inches='tight')
    plt.show()
    print('**********************')


def print_debug(text):
    if DEBUG:
        print(text)


def compute_mean_ci(x):
    mean_x = np.mean(x)
    stderr_x = scipy.stats.sem(x)
    # ci = (mean_x-1.98*stderr_x, mean_x+1.98*stderr_x)
    return mean_x, stderr_x


def plot_confusion(gts, preds, title):
    lab = sorted(np.unique(gts))
    cmat = confusion_matrix(gts, preds, labels=lab)
    # norm_cmat = cmat/np.sum(cmat, axis=1).reshape([-1, 1])
    plt.figure()
    plt.title(title)
    sns.heatmap(cmat, annot=True, fmt='d')
    plt.xlabel('Ground Truth'), plt.ylabel('Prediction')
    plt.show()